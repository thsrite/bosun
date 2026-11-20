#!/usr/bin/env python3
"""Read-only remote latency collector. Raw source records never reach disk or HTTP."""
from __future__ import annotations

import argparse
import concurrent.futures
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import statistics
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from analytics import enrich_calls, log_event, session_event, usage_event

ACTIVE = {"running", "waiting"}
STATUSES = ACTIVE | {"done", "failed", "cancelled", "interrupted", "paused", "queued"}
LIMITATIONS = [
    "只读监控；不调用模型，不重启、不切账号、不重放请求。",
    "CPA 未提供逐块收发与连接分段指标；慢流不能进一步区分模型端与出口链路。",
    "请求关联基于模型、Token 与时间；多候选不猜测。账号为本监控专用匿名标识。",
    "待完成状态根据最后会话事件推测，不证明模型正在生成；流式调用结束后才有完整耗时。",
    "输出速度为输出 Token / 首 Token 后时长的估计，包含推理口径差异，不是逐 Token 测速。",
    "未唯一关联的重试数量显示未知；关联成功且未见错误也不保证日志历史完整。",
]


class SourceError(Exception):
    pass


class Collector:
    def __init__(self, directory: Path, interval: float):
        self.directory = directory
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.interval = interval
        self.ssh_config = str(directory / "ssh_config")
        self.state_path = directory / "state.json"
        self.state = {"version": 1, "tasks": {}, "usage": [], "logs": [],
                      "cursors": {}, "usage_cursor": 0, "sources": {}}
        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text())
            if self.state.get("version") != 1:
                raise ValueError("unsupported monitor state version")
        salt_path = directory / "account_salt"
        if not salt_path.exists():
            salt_path.write_bytes(os.urandom(32))
            salt_path.chmod(0o600)
        self.salt = salt_path.read_bytes()
        self.buffers: dict[str, bytes] = {}
        self.lock = threading.Lock()
        self.snapshot: dict = {"updated_at": None, "interval_s": interval,
                               "sources": [], "tasks": [], "gateway_errors": [],
                               "limitations": LIMITATIONS}
        self.stopping = threading.Event()

    def ssh(self, host: str, command: str) -> bytes:
        try:
            p = subprocess.run(["ssh", "-F", self.ssh_config, host, command],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               timeout=15, check=False)
        except subprocess.TimeoutExpired:
            raise SourceError("ssh_timeout") from None
        if p.returncode:
            raise SourceError({64: "command_rejected", 65: "ambiguous_session",
                               66: "source_missing", 255: "ssh_access_failed"}
                              .get(p.returncode, "source_read_failed"))
        return p.stdout

    def lines(self, host: str, command: str, key: str):
        cursor = self.state["cursors"].setdefault(key, {"inode": None, "offset": 0})
        # Completed-line offsets alone are durable. Partial raw JSON stays only in RAM.
        for _ in range(4):
            pending = self.buffers.get(key, b"")
            offset = cursor["offset"] + len(pending)
            raw = self.ssh(host, f"{command} {offset}")
            header, separator, data = raw.partition(b"\n")
            if not separator:
                raise SourceError("invalid_source_header")
            try:
                inode, size, modified = map(int, header.split())
            except ValueError:
                raise SourceError("invalid_source_header") from None
            if cursor["inode"] not in (None, inode) or size < offset:
                cursor.update(inode=inode, offset=0)
                self.buffers.pop(key, None)
                continue
            cursor["inode"] = inode
            cursor["modified"] = modified
            combined = pending + data
            if len(combined) > 32 * 1024 * 1024:
                raise SourceError("record_exceeds_memory_bound")
            stop = combined.rfind(b"\n") + 1
            if stop:
                for line in combined[:stop].splitlines():
                    yield line
                cursor["offset"] += stop
            self.buffers[key] = combined[stop:]
            cursor["caught_up"] = offset + len(data) >= size
            if cursor["caught_up"]:
                break

    def poll_mac(self):
        rows = json.loads(self.ssh("mac", "tasks") or b"[]")
        now = time.time()
        tasks = self.state["tasks"]
        errors = []
        for row in rows:
            tid = str(int(row["id"]))
            uid = row.get("session_uid") or ""
            if not re.fullmatch(r"[0-9a-f-]{36}", uid):
                continue
            if tid not in tasks and row["status"] not in ACTIVE and tid != "1585":
                continue
            if tid not in tasks or tasks[tid]["session_uid"] != uid:
                tasks[tid] = {"id": int(tid), "session_uid": uid, "calls": [], "tools": [],
                              "last_event_at": None, "last_role": None}
            task = tasks[tid]
            task["status"] = row["status"] if row["status"] in STATUSES else "unknown"
            task["started_at"] = row.get("started_at") or 0
            task["observed_at"] = now
            key = f"session:{uid}"
            try:
                for line in self.lines("mac", f"session {uid}", key):
                    try:
                        event = session_event(json.loads(line))
                    except (ValueError, TypeError, KeyError, OverflowError):
                        errors.append("malformed_session_record")
                        continue
                    if event is None:
                        continue
                    at = event.get("end", event.get("start", event.get("at", 0))) or 0
                    if at < task["started_at"] - 2:
                        continue
                    task["last_event_at"] = max(task.get("last_event_at") or 0, at)
                    task["last_role"] = event["kind"]
                    if event["kind"] == "call":
                        event.pop("kind")
                        event["task_id"] = int(tid)
                        task["calls"] = [x for x in task["calls"] if x["id"] != event["id"]]
                        task["calls"].append(event)
                        task["calls"] = task["calls"][-250:]
                    elif event["kind"] in {"tool_start", "tool_end"}:
                        existing = next((x for x in task["tools"] if x["id"] == event["id"]), None)
                        if existing is None:
                            existing = {"id": event["id"], "name": event.get("name", "unknown")}
                            task["tools"].append(existing)
                        existing.update({k: v for k, v in event.items() if k != "kind"})
                        task["tools"] = task["tools"][-500:]
                task["caught_up"] = self.state["cursors"][key].get("caught_up", False)
                task["source_ok"] = True
            except SourceError as exc:
                task["source_ok"] = False
                errors.append(str(exc))
        live_ids = {str(row["id"]) for row in rows}
        for tid, task in tasks.items():
            if tid not in live_ids:
                task["source_ok"] = False
                task["status"] = "unobserved"
        if errors:
            raise SourceError(errors[0])

    def poll_usage(self):
        for _ in range(8):
            cursor = self.state["usage_cursor"]
            rows = json.loads(self.ssh("cpa", f"usage {cursor}") or b"[]")
            normalized = [usage_event(row, self.salt) for row in rows]
            if any(row is None or row.get("start") is None for row in normalized):
                raise SourceError("malformed_usage_record")
            self.state["usage"].extend(normalized)
            if rows:
                self.state["usage_cursor"] = max(int(x["id"]) for x in rows)
            if len(rows) < 2000:
                return
        raise SourceError("backfill_in_progress")

    def poll_log(self):
        for line in self.lines("cpa", "log", "cpa_log"):
            event = log_event(line.decode("utf-8", errors="replace"), self.salt)
            if event:
                self.state["logs"].append(event)

    def source(self, name, function):
        status = self.state["sources"].setdefault(name, {"name": name, "last_success": None})
        try:
            function()
        except SourceError as exc:
            status.update(ok=False, error=str(exc))
        except (ValueError, TypeError, KeyError, OSError):
            status.update(ok=False, error="invalid_source_data")
        else:
            status.update(ok=True, error=None, last_success=time.time())

    def cycle(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(self.source, name, method) for name, method in [
                ("OMP / Bosun", self.poll_mac), ("CPA usage", self.poll_usage), ("CPA log", self.poll_log)]]
            for future in futures:
                future.result()
        now = time.time()
        cutoff = now - 86400
        self.state["usage"] = [x for x in self.state["usage"] if x["start"] >= cutoff][-12000:]
        self.state["logs"] = [x for x in self.state["logs"] if x["at"] >= cutoff][-20000:]
        tasks = self.state["tasks"]
        for tid in list(tasks):
            task = tasks[tid]
            if task["status"] not in ACTIVE and (task.get("last_event_at") or 0) < cutoff:
                del tasks[tid]
        all_calls = [call for task in tasks.values() for call in task["calls"]]
        enriched = enrich_calls(all_calls, self.state["usage"], self.state["logs"])
        keyed = {(call["task_id"], call["id"]): call for call in enriched}
        views = []
        for task in tasks.values():
            calls = [dict(keyed[(task["id"], x["id"])]) for x in task["calls"]]
            tools = task["tools"]
            for call in calls:
                next_start = min((x["start"] for x in calls if x["start"] > call["start"]), default=now)
                following = [x for x in tools if call["end"] - 1 <= x.get("start", -1) < next_start]
                failed = [x for x in following if x.get("error")]
                call["tool_errors"] = len(failed)
                call["tool_duration_s"] = max((x["end"] - x["start"] for x in following
                                                if "end" in x and "start" in x), default=None)
                if failed:
                    call["diagnosis"].append("后续工具失败；可能增加返工轮次")
            durations = [c["duration_s"] for c in calls if c.get("duration_s") is not None]
            pending = None
            if task["status"] in ACTIVE and task.get("caught_up") and task.get("source_ok"):
                since = task.get("last_event_at")
                if since:
                    last_call = max(calls, key=lambda x: x["end"], default=None)
                    # A completed assistant stop means no evidence of an outstanding model request.
                    if not (task.get("last_role") == "call" and last_call and last_call.get("stop") == "stop"):
                        unfinished = [t for t in tools if "end" not in t and t.get("start", 0) >= (last_call or {}).get("start", 0)]
                        pending = {"kind": "tool" if unfinished else "awaiting_model_or_local",
                                   "since": since, "elapsed_s": max(0, now - since)}
            views.append({"id": task["id"], "status": task["status"],
                          "last_event_at": task.get("last_event_at"), "pending": pending,
                          "caught_up": task.get("caught_up", False), "source_ok": task.get("source_ok", False),
                          "calls": sorted(calls, key=lambda c: c["start"], reverse=True),
                          "summary": {"calls": len(calls), "median_s": statistics.median(durations) if durations else None,
                                      "slow_calls": sum(d >= 30 for d in durations)}})
        snapshot = {"updated_at": now, "interval_s": self.interval,
                    "sources": [dict(source) for source in self.state["sources"].values()],
                    "tasks": sorted(views, key=lambda t: t["id"], reverse=True),
                    "gateway_errors": [{"at": x["at"], "kind": x["error_kind"], "stream_id": x["stream_id"]}
                                       for x in self.state["logs"] if x["kind"] == "failure"][-40:],
                    "limitations": LIMITATIONS}
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state, ensure_ascii=False, separators=(",", ":")))
        temporary.chmod(0o600)
        temporary.replace(self.state_path)
        with self.lock:
            self.snapshot = snapshot
        print(json.dumps({"at": now, "tasks": len(views), "calls": len(all_calls),
                          "sources_ok": {x["name"]: x["ok"] for x in snapshot["sources"]}}), flush=True)

    def run(self):
        while not self.stopping.is_set():
            start = time.monotonic()
            try:
                self.cycle()
            except Exception as exc:
                # Do not include exception messages: libraries may embed source data.
                print(json.dumps({"collector_error": type(exc).__name__}), flush=True)
            self.stopping.wait(max(1, self.interval - (time.monotonic() - start)))


def serve(collector: Collector, host: str, port: int):
    document = (Path(__file__).parent / "index.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            peer = ipaddress.ip_address(self.client_address[0])
            if not (peer.is_loopback or peer in ipaddress.ip_network("10.0.0.0/24")):
                self.send_error(403)
                return
            if self.path in {"/", "/index.html"}:
                body, kind = document, "text/html; charset=utf-8"
            elif self.path == "/api/status":
                with collector.lock:
                    body = json.dumps(collector.snapshot, ensure_ascii=False).encode()
                kind = "application/json; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    worker = threading.Thread(target=collector.run, daemon=True)
    worker.start()
    server.timeout = 1
    try:
        while not collector.stopping.is_set():
            server.handle_request()
    finally:
        collector.stopping.set()
        server.server_close()
        worker.join(timeout=30)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=Path("/var/lib/bosun-latency-monitor"))
    parser.add_argument("--interval", type=float, default=10)
    parser.add_argument("--host", default="10.0.0.49")
    parser.add_argument("--port", type=int, default=18785)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.interval < 5:
        parser.error("minimum interval is 5 seconds")
    os.umask(0o077)
    collector = Collector(args.state_dir, args.interval)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: collector.stopping.set())
    if args.once:
        collector.cycle()
        print(json.dumps(collector.snapshot, ensure_ascii=False))
    else:
        serve(collector, args.host, args.port)


if __name__ == "__main__":
    main()
