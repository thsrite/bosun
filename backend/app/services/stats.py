"""Canonical stats and self-diagnosis aggregate for Bosun."""
from __future__ import annotations

import os
import re
import shutil
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from .. import db

try:  # psutil is the preferred source, but host metrics must degrade cleanly.
    import psutil  # type: ignore
except Exception:  # pragma: no cover - depends on the host environment
    psutil = None  # type: ignore


def _pct(part: int | float, whole: int | float) -> float:
    return round((part / whole) * 100, 1) if whole else 0.0


def _metric_pct(value: int | float | None) -> float | None:
    if value is None:
        return None
    return round(max(0.0, min(100.0, float(value))), 1)


def _metric_float(value: int | float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 1)


def _day(ts: float) -> int:
    return int(ts // 86400)


def _setting_float(key: str, default: float = 0.0) -> float:
    try:
        return float(db.get_setting(key, default))
    except (TypeError, ValueError):
        return default


def _count_map(sql: str, params: tuple = ()) -> dict[str, int]:
    return {str(r["k"]): int(r["c"] or 0) for r in db.query(sql, params)}


def _durations(rows: list[dict[str, Any]]) -> list[float]:
    vals = []
    for r in rows:
        start, end = r.get("started_at"), r.get("ended_at")
        if start is None or end is None:
            continue
        dur = max(0.0, float(end) - float(start))
        vals.append(dur)
    return vals


def _task_rows(since: float | None = None) -> list[dict[str, Any]]:
    if since is None:
        rows = db.query("SELECT * FROM task WHERE deleted=0")
    else:
        rows = db.query("SELECT * FROM task WHERE deleted=0 AND created_at>=?", (since,))
    return [dict(r) for r in rows]


def _psutil_cpu_temp_c() -> float | None:
    if psutil is None:
        return None
    try:
        sensors = psutil.sensors_temperatures(fahrenheit=False)
    except Exception:
        return None
    preferred: list[float] = []
    fallback: list[float] = []
    for name, entries in (sensors or {}).items():
        for entry in entries:
            value = getattr(entry, "current", None)
            if value is None:
                continue
            try:
                temp = float(value)
            except (TypeError, ValueError):
                continue
            if not 0 < temp < 125:
                continue
            label = f"{name} {getattr(entry, 'label', '')}".lower()
            if any(key in label for key in ("cpu", "core", "package", "soc", "tctl", "tdie")):
                preferred.append(temp)
            else:
                fallback.append(temp)
    values = preferred or fallback
    return max(values) if values else None


def _parse_temperature(text: str) -> float | None:
    match = re.search(r"(-?\d+(?:\.\d+)?)\s*(?:°?\s*[cC])?", text)
    if not match:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    return value if 0 < value < 125 else None


def _parse_vorssaint_cpu_temp(text: str) -> float | None:
    """Return the hottest CPU core reported by ``Vorssaint --sensors``."""
    temperatures: list[float] = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 4 or fields[0] != "cpu-core":
            continue
        try:
            value = float(fields[-1])
        except ValueError:
            continue
        if 0 < value < 125:
            temperatures.append(value)
    return max(temperatures) if temperatures else None


def _candidate_executables(binary: str) -> list[str]:
    seen: set[str] = set()
    candidates = [
        shutil.which(binary),
        *(str(path) for path in Path.home().glob(f".gem/ruby/*/bin/{binary}")),
        f"/opt/homebrew/bin/{binary}",
        f"/usr/local/bin/{binary}",
        f"/usr/bin/{binary}",
        f"/usr/sbin/{binary}",
    ]
    out = []
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        if Path(candidate).exists():
            seen.add(candidate)
            out.append(candidate)
    return out


def _vorssaint_cpu_temp_c() -> float | None:
    if sys.platform != "darwin":
        return None
    candidates = [
        Path("/Applications/Vorssaint.app/Contents/MacOS/Vorssaint"),
        Path.home() / "Applications/Vorssaint.app/Contents/MacOS/Vorssaint",
    ]
    for executable in candidates:
        if not executable.is_file():
            continue
        try:
            proc = subprocess.run(
                (str(executable), "--sensors"),
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
        except Exception:
            continue
        temp = _parse_vorssaint_cpu_temp(proc.stdout)
        if temp is not None:
            return temp
    return None


def _command_cpu_temp_c() -> float | None:
    commands = [
        ("osx-cpu-temp", ()),
        ("istats", ("cpu", "temp", "--value-only")),
    ]
    for binary, args in commands:
        for executable in _candidate_executables(binary):
            try:
                proc = subprocess.run(
                    (executable, *args),
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=1,
                )
            except Exception:
                continue
            temp = _parse_temperature(f"{proc.stdout}\n{proc.stderr}")
            if temp is not None:
                return temp
    return None


def _linux_thermal_cpu_temp_c() -> float | None:
    root = Path("/sys/class/thermal")
    if not root.exists():
        return None
    preferred: list[float] = []
    fallback: list[float] = []
    for temp_path in root.glob("thermal_zone*/temp"):
        try:
            raw = float(temp_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            continue
        temp = raw / 1000 if raw > 1000 else raw
        if not 0 < temp < 125:
            continue
        try:
            label = temp_path.with_name("type").read_text(encoding="utf-8").lower()
        except OSError:
            label = ""
        if any(key in label for key in ("cpu", "x86_pkg", "package", "soc", "tctl", "tdie")):
            preferred.append(temp)
        else:
            fallback.append(temp)
    values = preferred or fallback
    return max(values) if values else None


def _cpu_temp_c() -> float | None:
    return _metric_float(
        _psutil_cpu_temp_c()
        or _linux_thermal_cpu_temp_c()
        or _vorssaint_cpu_temp_c()
        or _command_cpu_temp_c()
    )


def _cpu_load_pct() -> float | None:
    if psutil is not None:
        try:
            return _metric_pct(psutil.cpu_percent(interval=0.05))
        except Exception:
            pass
    try:
        load_1m = os.getloadavg()[0]
    except (AttributeError, OSError):
        return None
    return _metric_pct((load_1m / max(1, os.cpu_count() or 1)) * 100)


def _memory_load_pct() -> float | None:
    if psutil is not None:
        try:
            return _metric_pct(psutil.virtual_memory().percent)
        except Exception:
            pass
    linux = _linux_memory_load_pct()
    if linux is not None:
        return linux
    return _darwin_memory_load_pct()


def _linux_memory_load_pct() -> float | None:
    meminfo = Path("/proc/meminfo")
    if not meminfo.exists():
        return None
    values: dict[str, float] = {}
    try:
        for line in meminfo.read_text(encoding="utf-8").splitlines():
            key, _, rest = line.partition(":")
            if key in {"MemTotal", "MemAvailable"}:
                values[key] = float(rest.strip().split()[0])
    except (OSError, ValueError, IndexError):
        return None
    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    if not total or available is None:
        return None
    return _metric_pct(((total - available) / total) * 100)


def _parse_vm_stat_memory_pct(vm_stat_text: str, total_bytes: int | float) -> float | None:
    page_match = re.search(r"page size of\s+(\d+)\s+bytes", vm_stat_text, flags=re.IGNORECASE)
    if not page_match or total_bytes <= 0:
        return None
    page_size = float(page_match.group(1))
    pages: dict[str, float] = {}
    for line in vm_stat_text.splitlines():
        key, sep, rest = line.partition(":")
        if not sep:
            continue
        match = re.search(r"(\d+)", rest)
        if not match:
            continue
        pages[key.strip().strip('"').lower()] = float(match.group(1))
    available_pages = (
        pages.get("pages free", 0.0)
        + pages.get("pages inactive", 0.0)
        + pages.get("pages speculative", 0.0)
    )
    if available_pages <= 0:
        return None
    used_bytes = max(0.0, float(total_bytes) - available_pages * page_size)
    return _metric_pct((used_bytes / float(total_bytes)) * 100)


def _darwin_memory_load_pct() -> float | None:
    if sys.platform != "darwin":
        return None
    try:
        sysctl = _candidate_executables("sysctl")[0]
        vm_stat = _candidate_executables("vm_stat")[0]
    except IndexError:
        return None
    try:
        total_proc = subprocess.run(
            (sysctl, "-n", "hw.memsize"),
            check=False,
            capture_output=True,
            text=True,
            timeout=1,
        )
        total = float(total_proc.stdout.strip())
        vm_proc = subprocess.run(
            (vm_stat,),
            check=False,
            capture_output=True,
            text=True,
            timeout=1,
        )
    except Exception:
        return None
    return _parse_vm_stat_memory_pct(vm_proc.stdout, total)


def _disk_load_pct() -> float | None:
    path = os.environ.get("BOSUN_HOST_DISK_PATH") or os.path.abspath(os.sep)
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    return _metric_pct((usage.used / usage.total) * 100 if usage.total else None)


def host_metrics(now: float | None = None) -> dict:
    """Best-effort host telemetry for the header status strip."""
    return {
        "generated_at": now or time.time(),
        "cpu_temp_c": _cpu_temp_c(),
        "cpu_load_pct": _cpu_load_pct(),
        "memory_load_pct": _memory_load_pct(),
        "disk_load_pct": _disk_load_pct(),
    }


def _summary(tasks: list[dict[str, Any]], all_tasks: list[dict[str, Any]], now: float) -> dict:
    by_status: dict[str, int] = defaultdict(int)
    for task in all_tasks:
        by_status[str(task["status"])] += 1
    done = by_status.get("done", 0)
    failed = by_status.get("failed", 0)
    finished = done + failed
    durs = _durations([t for t in all_tasks if t.get("status") in {"done", "failed"}])
    # 健康分口径(堵刷分):
    #  - failed 只计窗口内(tasks), 避免历史累计失败无限期钝化分数
    #  - 未闭合问题 = open + muted(放弃≠解决) + 已转任务但任务未完成; task_created 不再免罚
    failed_window = sum(1 for t in tasks if str(t.get("status")) == "failed")
    unresolved = db.query_one(
        "SELECT COUNT(*) c FROM finding f LEFT JOIN task t ON t.id=f.task_id "
        "WHERE f.status IN ('open','muted') "
        "OR (f.status='task_created' AND (f.task_id IS NULL OR t.status!='done'))"
    )["c"]
    stopped_ap = db.query_one(
        "SELECT COUNT(*) c FROM autopilot_run WHERE status IN ('failed','stopped')"
    )["c"]
    stale = 0
    for p in db.query("SELECT id FROM project"):
        last = _setting_float(f"analyze_at:{p['id']}", 0)
        if not last or now - last > 3 * 86400:
            stale += 1
    health = max(0, round(100 - min(35, failed_window * 8) - min(45, unresolved * 5) - min(10, stopped_ap * 3) - min(10, stale * 2)))
    return {
        "projects": db.query_one("SELECT COUNT(*) c FROM project")["c"],
        "total_tasks": len(all_tasks),
        "active_tasks": sum(by_status.get(s, 0) for s in ("running", "waiting_input", "queued")),
        "by_status": dict(by_status),
        "success_rate": _pct(done, finished),
        "failure_rate": _pct(failed, finished),
        "avg_duration_sec": round(statistics.mean(durs), 1) if durs else 0.0,
        "median_duration_sec": round(statistics.median(durs), 1) if durs else 0.0,
        "health_score": health,
        "deleted_tasks": db.query_one("SELECT COUNT(*) c FROM task WHERE deleted=1")["c"],
    }


def _throughput(tasks: list[dict[str, Any]], since: float, days: int) -> list[dict]:
    days_map = {
        _day(since + i * 86400): {"created": 0, "done": 0, "failed": 0}
        for i in range(days + 1)
    }
    for t in tasks:
        d = _day(t["created_at"])
        row = days_map.setdefault(d, {"created": 0, "done": 0, "failed": 0})
        row["created"] += 1
        if t["status"] == "done":
            row["done"] += 1
        elif t["status"] == "failed":
            row["failed"] += 1
    return [{"date": d * 86400, **v} for d, v in sorted(days_map.items()) if d * 86400 >= since]


def _engine_quality(tasks: list[dict[str, Any]]) -> list[dict]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in tasks:
        groups[str(t["engine"])].append(t)
    out = []
    for engine, rows in groups.items():
        done = sum(1 for r in rows if r["status"] == "done")
        failed = sum(1 for r in rows if r["status"] == "failed")
        finished = done + failed
        tokens = sum(int(r["tokens"] or 0) for r in rows)
        durs = _durations([r for r in rows if r["status"] in {"done", "failed"}])
        out.append({
            "engine": engine,
            "tasks": len(rows),
            "done": done,
            "failed": failed,
            "success_rate": _pct(done, finished),
            "failure_rate": _pct(failed, finished),
            "avg_duration_sec": round(statistics.mean(durs), 1) if durs else 0.0,
            "avg_tokens": round(tokens / len(rows), 1) if rows else 0,
            "tokens": tokens,
        })
    return sorted(out, key=lambda r: (-r["tasks"], r["engine"]))


def _finding_health(now: float) -> dict:
    open_row = db.query_one("SELECT MIN(created_at) oldest, COUNT(*) c FROM finding WHERE status='open'")
    repeated = [
        dict(r)
        for r in db.query(
            "SELECT project_id,source,title,attempts,muted,updated_at FROM fix_memory "
            "ORDER BY attempts DESC, updated_at DESC LIMIT 15"
        )
    ]
    return {
        "by_source": _count_map("SELECT source k, COUNT(*) c FROM finding GROUP BY source"),
        "by_severity": _count_map("SELECT severity k, COUNT(*) c FROM finding GROUP BY severity"),
        "by_status": _count_map("SELECT status k, COUNT(*) c FROM finding GROUP BY status"),
        "by_origin": _count_map("SELECT origin k, COUNT(*) c FROM finding GROUP BY origin"),
        "open": int(open_row["c"] or 0),
        "muted": db.query_one("SELECT COUNT(*) c FROM fix_memory WHERE muted=1")["c"],
        "oldest_open_age_sec": round(now - open_row["oldest"], 1) if open_row and open_row["oldest"] else 0,
        "repeated": repeated,
    }


def _token_economics(tasks: list[dict[str, Any]], since: float) -> dict:
    project_rows = db.query(
        "SELECT p.id, p.name, "
        "COALESCE((SELECT SUM(tokens) FROM task WHERE project_id=p.id AND tokens IS NOT NULL),0) task_tokens, "
        "COALESCE((SELECT SUM(tokens_used) FROM autopilot_run WHERE project_id=p.id),0) autopilot_tokens "
        "FROM project p"
    )
    by_project = []
    for r in project_rows:
        total = int(r["task_tokens"] or 0) + int(r["autopilot_tokens"] or 0)
        if total:
            by_project.append({
                "project_id": r["id"],
                "name": r["name"],
                "task_tokens": int(r["task_tokens"] or 0),
                "autopilot_tokens": int(r["autopilot_tokens"] or 0),
                "tokens": total,
            })
    by_project.sort(key=lambda r: -r["tokens"])
    by_engine: dict[str, int] = defaultdict(int)
    for t in tasks:
        by_engine[str(t["engine"])] += int(t["tokens"] or 0)
    total_task = sum(int(t["tokens"] or 0) for t in tasks)
    total_ap = db.query_one("SELECT COALESCE(SUM(tokens_used),0) t FROM autopilot_run")["t"] or 0
    done = sum(1 for t in tasks if t["status"] == "done")
    days_map: dict[int, dict[str, int]] = {}
    for t in tasks:
        ended = t.get("ended_at")
        if not ended or ended < since:
            continue
        days_map.setdefault(_day(ended), {"task_tokens": 0, "autopilot_tokens": 0})["task_tokens"] += int(t["tokens"] or 0)
    for r in db.query("SELECT ended_at,tokens_used FROM autopilot_run WHERE ended_at IS NOT NULL AND ended_at>=?", (since,)):
        days_map.setdefault(_day(r["ended_at"]), {"task_tokens": 0, "autopilot_tokens": 0})["autopilot_tokens"] += int(r["tokens_used"] or 0)
    return {
        "by_project": by_project[:10],
        "by_engine": dict(by_engine),
        "total": int(total_task + total_ap),
        "task_tokens": int(total_task),
        "autopilot_tokens": int(total_ap),
        "token_per_success": round((total_task + total_ap) / done, 1) if done else 0,
        "timeline": [{"date": d * 86400, **v} for d, v in sorted(days_map.items())],
    }


def _project_activity() -> list[dict]:
    rows = db.query(
        "SELECT p.id, p.name, COUNT(t.id) tasks FROM project p "
        "LEFT JOIN task t ON t.project_id=p.id AND t.deleted=0 "
        "GROUP BY p.id ORDER BY tasks DESC, p.name ASC LIMIT 10"
    )
    return [{"project_id": r["id"], "name": r["name"], "tasks": r["tasks"]} for r in rows]


def _project_risk(now: float) -> list[dict]:
    out = []
    for p in db.query("SELECT * FROM project"):
        failed = db.query_one("SELECT COUNT(*) c FROM task WHERE project_id=? AND status='failed' AND deleted=0", (p["id"],))["c"]
        open_findings = db.query_one("SELECT COUNT(*) c FROM finding WHERE project_id=? AND status='open'", (p["id"],))["c"]
        muted = db.query_one("SELECT COUNT(*) c FROM fix_memory WHERE project_id=? AND muted=1", (p["id"],))["c"]
        ap_failed = db.query_one("SELECT COUNT(*) c FROM autopilot_run WHERE project_id=? AND status IN ('failed','stopped')", (p["id"],))["c"]
        tokens = db.query_one("SELECT COALESCE(SUM(tokens),0) t FROM task WHERE project_id=? AND deleted=0", (p["id"],))["t"] or 0
        last_analyze = _setting_float(f"analyze_at:{p['id']}", 0)
        stale = not last_analyze or now - last_analyze > 3 * 86400
        score = failed * 10 + open_findings * 8 + muted * 8 + ap_failed * 6 + (6 if stale else 0) + min(10, int(tokens // 10000))
        reasons = []
        if failed:
            reasons.append(f"{failed} 个失败任务")
        if open_findings:
            reasons.append(f"{open_findings} 个待处理问题")
        if muted:
            reasons.append(f"{muted} 个重复失败记忆")
        if ap_failed:
            reasons.append(f"{ap_failed} 次自愈失败/停止")
        if stale:
            reasons.append("项目分析已过期")
        if score:
            out.append({
                "project_id": p["id"],
                "name": p["name"],
                "score": score,
                "reasons": reasons,
                "open_findings": open_findings,
                "failed_tasks": failed,
                "tokens": int(tokens),
                "last_analyze_at": last_analyze or None,
                "stale_analysis": stale,
            })
    return sorted(out, key=lambda r: (-r["score"], r["name"]))[:10]


def _bottlenecks(tasks: list[dict[str, Any]], engine_quality: list[dict], now: float) -> list[dict]:
    out = []
    for t in tasks:
        if t["status"] == "waiting_input" and t.get("started_at") and now - t["started_at"] > 1800:
            out.append({
                "severity": "warning",
                "title": f"任务 #{t['id']} 等待输入过久",
                "detail": "任务处于 waiting_input 超过 30 分钟，占用并发槽。",
                "action": "打开任务，根据建议回复或手动完成/取消。",
                "task_id": t["id"],
                "project_id": t["project_id"],
            })
        if t["status"] == "queued" and now - t["created_at"] > 3600:
            out.append({
                "severity": "info",
                "title": f"任务 #{t['id']} 排队过久",
                "detail": "任务排队超过 60 分钟。",
                "action": "检查并发上限、优先级或卡住的运行任务。",
                "task_id": t["id"],
                "project_id": t["project_id"],
            })
    for e in engine_quality:
        if e["failed"] >= 1 and e["failure_rate"] >= 40:
            out.append({
                "severity": "warning",
                "title": f"{e['engine']} 失败率偏高",
                "detail": f"{e['engine']} 失败率为 {e['failure_rate']}%。",
                "action": "检查路由策略、prompt 模板，或将高风险任务切到另一个引擎。",
            })
    return out[:12]


def _insights(summary: dict, finding_health: dict, engine_quality: list[dict], project_risk: list[dict], token_economics: dict, bottlenecks: list[dict]) -> list[dict]:
    out = []
    if summary["health_score"] < 80:
        out.append({
            "severity": "warning",
            "title": "系统健康分下降",
            "detail": f"当前健康分 {summary['health_score']}，主要由失败任务、open finding 或过期分析拉低。",
            "action": "优先处理项目风险排行前几项，再运行整体分析刷新问题状态。",
        })
    if finding_health["open"]:
        out.append({
            "severity": "warning",
            "title": "存在待处理问题",
            "detail": f"当前有 {finding_health['open']} 个 open finding。",
            "action": "进入问题收件箱，批量转修复任务或忽略不再处理的问题。",
        })
    worst_engine = next((e for e in engine_quality if e["failure_rate"] >= 40 and e["failed"] > 0), None)
    if worst_engine:
        out.append({
            "severity": "warning",
            "title": f"{worst_engine['engine']} 任务质量偏低",
            "detail": f"{worst_engine['engine']} 完成任务成功率 {worst_engine['success_rate']}%。",
            "action": "将复杂修复临时路由到成功率更高的引擎，并检查失败日志中的共性。",
        })
    if token_economics["token_per_success"] >= 10000:
        out.append({
            "severity": "info",
            "title": "单次成功 token 成本偏高",
            "detail": f"每个成功任务平均消耗 {token_economics['token_per_success']} tokens。",
            "action": "拆小任务、缩短 prompt，并让反思提案检查策略频率。",
        })
    if project_risk:
        out.append({
            "severity": "critical" if project_risk[0]["score"] >= 25 else "warning",
            "title": f"{project_risk[0]['name']} 风险最高",
            "detail": "；".join(project_risk[0]["reasons"]) or "风险分最高。",
            "action": "先处理该项目的 open finding、失败任务和过期分析。",
        })
    if bottlenecks:
        out.append({
            "severity": "warning",
            "title": "存在执行瓶颈",
            "detail": bottlenecks[0]["detail"],
            "action": bottlenecks[0]["action"],
        })
    return out[:8]


def build_dashboard(days: int = 30, now: float | None = None) -> dict:
    days = max(7, min(90, int(days or 30)))
    now = now or time.time()
    since = now - days * 86400
    all_tasks = _task_rows()
    window_tasks = [t for t in all_tasks if t["created_at"] >= since]
    summary = _summary(window_tasks, all_tasks, now)
    throughput = _throughput(window_tasks, since, days)
    engine_quality = _engine_quality(all_tasks)
    finding_health = _finding_health(now)
    token_economics = _token_economics(all_tasks, since)
    project_risk = _project_risk(now)
    bottlenecks = _bottlenecks(all_tasks, engine_quality, now)
    return {
        "generated_at": now,
        "window": {"days": days, "since": since},
        "summary": summary,
        "throughput": throughput,
        "engine_quality": engine_quality,
        "finding_health": finding_health,
        "token_economics": token_economics,
        "project_risk": project_risk,
        "project_activity": _project_activity(),
        "bottlenecks": bottlenecks,
        "host": host_metrics(now),
        "insights": _insights(summary, finding_health, engine_quality, project_risk, token_economics, bottlenecks),
    }
