"""自愈编排器：审 → 修 → 验 → 交叉复审 → 提交，循环直到清零/无进展/触顶。

安全：全程在专用分支 bosun/autopilot-<ts> 上，每轮 commit，可 git reset 回滚。
验证：客观信号(test/build/lint 真实红绿) + 另一引擎交叉复审 git diff。
修复：headless 模式(claude -p / codex exec)跑完即退出，适合自动化。
"""
from __future__ import annotations

import re
import subprocess
import threading
import time

from . import db, engine_settings, events
from .config import LOG_DIR
from .engines import build_headless_argv
from .services import discovery

FIX_TIMEOUT = 900
REVIEW_TIMEOUT = 300

_stops: dict[int, threading.Event] = {}


def _cmd(cwd: str, argv: list[str], timeout: int) -> tuple[int, str]:
    from .env import child_env

    try:
        p = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=child_env())
        return p.returncode, (p.stdout + p.stderr)
    except subprocess.TimeoutExpired:
        return -1, "(timeout)"
    except Exception as exc:  # noqa: BLE001
        return -1, str(exc)


def _git(cwd: str, *args: str) -> tuple[int, str]:
    return _cmd(cwd, ["git", *args], 60)


def _parse_tokens(engine: str, stdout: str) -> int:
    """从 headless 结构化输出解析 token 用量(input+output，不含 cache，全局口径一致)。"""
    import json

    if engine == "cc":
        try:
            u = (json.loads(stdout) or {}).get("usage") or {}
        except (json.JSONDecodeError, TypeError):
            return 0
        return sum(v for k, v in u.items() if isinstance(v, int) and k in ("input_tokens", "output_tokens"))
    if engine == "omp":
        # omp --mode json 是逐事件 NDJSON，每条消息自带增量 usage。同一条消息会在
        # message_start/message_end/turn_end/agent_end 里重复出现，只认 message_end
        # 累加，否则会重复计数。
        total = 0
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict) or obj.get("type") != "message_end":
                continue
            msg = obj.get("message")
            u = msg.get("usage") if isinstance(msg, dict) else None
            if isinstance(u, dict):
                total += sum(v for k, v in u.items() if isinstance(v, int) and k in ("input", "output"))
        return total
    # codex JSONL：逐事件找含 token 的 usage，取累计最大
    best = 0
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        stack = [obj]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                s = sum(v for k, v in cur.items() if isinstance(v, int) and "token" in k.lower())
                best = max(best, s)
                stack.extend(cur.values())
            elif isinstance(cur, list):
                stack.extend(cur)
    return best


def _headless(run_id: int, engine: str, prompt: str, cwd: str, timeout: int) -> tuple[int, str, int]:
    """跑一次 headless，累加 token 用量到 run。返回 (code, out, tokens)。

    cc 走 Agent SDK(精确 token/成败，无需刮屏)；codex 无 SDK，仍用 subprocess 解析。
    """
    if engine_settings.should_use_claude_sdk(engine, resume=False, post_input=None):
        from . import sdk_run

        res = sdk_run.run_sync(prompt, cwd, auto_approve=True, timeout=timeout)
        code = 1 if res["is_error"] else 0
        out = res["text"]
        toks = res["tokens"] or (len(prompt) + len(out)) // 4
    else:
        code, out = _cmd(cwd, build_headless_argv(engine, prompt, json_out=True), timeout)
        toks = _parse_tokens(engine, out) or (len(prompt) + len(out)) // 4
    db.execute("UPDATE autopilot_run SET tokens_used = tokens_used + ? WHERE id=?", (toks, run_id))
    events.emit("autopilot.update", {"run_id": run_id})
    return code, out, toks


def _over_budget(run_id: int, budget: int) -> bool:
    if budget <= 0:
        return False
    row = db.query_one("SELECT tokens_used FROM autopilot_run WHERE id=?", (run_id,))
    return bool(row) and row["tokens_used"] >= budget


def _log(run_id: int, msg: str) -> None:
    row = db.query_one("SELECT log_path FROM autopilot_run WHERE id=?", (run_id,))
    if row and row["log_path"]:
        try:
            with open(row["log_path"], "a") as f:
                f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        except OSError:
            pass
    events.emit("autopilot.log", {"run_id": run_id, "line": msg})


def _span(run_id, iteration, stage, label, status, started, ended, tokens=0, detail="") -> None:
    db.execute(
        "INSERT INTO autopilot_span(run_id,iteration,stage,label,status,tokens,detail,started_at,ended_at)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (run_id, iteration, stage, label, status, tokens, detail, started, ended),
    )
    events.emit("autopilot.span", {"run_id": run_id})


def _finish(run_id: int, status: str, summary: str) -> None:
    db.execute(
        "UPDATE autopilot_run SET status=?, summary=?, ended_at=? WHERE id=?",
        (status, summary, time.time(), run_id),
    )
    _log(run_id, f"[结束:{status}] {summary}")
    events.emit("autopilot.update", {"run_id": run_id, "status": status})
    _stops.pop(run_id, None)


def _repair_prompt(f) -> str:
    prompt = (
        f"修复以下问题（来源 {f['source']}，严重度 {f['severity']}）。只做必要的最小改动：\n"
        f"{f['title']}"
    )
    detail = (f["detail"] or "").strip()
    # 外部单字段源 title 与 detail 是同一段文字，相同时不重复拼「详情」
    if detail and detail != (f["title"] or "").strip():
        prompt += f"\n\n详情:\n{detail}"
    return prompt


def _verify(project) -> tuple[str, str]:
    """跑配置的 test/build/lint。返回 (result, detail)，result ∈ pass|fail|skip。

    skip = 未配置验证命令：无客观信号，不足以证明修复正确，**不等同 pass**。
    """
    cfg = db.query_one("SELECT * FROM project_config WHERE project_id=?", (project["id"],))
    if cfg is None:
        return "skip", "(无验证配置)"
    ran = []
    for name, cmd in (("test", cfg["test_cmd"]), ("build", cfg["build_cmd"]), ("lint", cfg["lint_cmd"])):
        if not cmd:
            continue
        code, _ = _cmd(project["path"], ["sh", "-c", cmd], FIX_TIMEOUT)
        ran.append(f"{name}={'ok' if code == 0 else 'fail'}")
        if code != 0:
            return "fail", " ".join(ran)
    return ("pass", " ".join(ran)) if ran else ("skip", "(无验证命令)")


def _text_blocks(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict) and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "\n".join(parts)


def assistant_text(engine: str, stdout: str) -> str:
    """从 headless 的结构化输出里抽出助手正文，丢掉被回显的用户提示词。

    结论判定必须基于这段正文：原始事件流里既有用户提示词也有 diff 原文，
    对它做 PASS/FAIL 正则会读到别人的字。
    """
    import json

    if engine == "cc":
        try:
            parsed = json.loads(stdout)
        except (json.JSONDecodeError, TypeError):
            return stdout
        text = parsed.get("result") or parsed.get("text") if isinstance(parsed, dict) else None
        return text if isinstance(text, str) else stdout

    parts: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        if engine == "omp":
            # omp --mode json：同一条消息会在多种事件里重复出现，只认 message_end
            if obj.get("type") != "message_end":
                continue
            msg = obj.get("message")
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                parts.append(_text_blocks(msg.get("content")))
            continue
        # codex exec --json：助手正文在 item.completed 事件里(实测 codex-cli 0.146.0)
        #   {"type":"item.completed","item":{"type":"agent_message","text":"..."}}
        if obj.get("type") == "item.completed":
            item = obj.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
    return "\n".join(p for p in parts if p).strip()


def _cross_review(run_id: int, engine: str, cwd: str, diff: str) -> tuple[str, str]:
    """返回 (verdict, note)，verdict ∈ pass|fail|unknown。

    fail-open 收敛：空 diff / 复审未运行 / 无明确结论 一律 unknown(不放行)，不再默认 pass。
    """
    if not diff.strip():
        return "unknown", "(无改动，无法复审)"
    prompt = (
        "以下是一次自动修复产生的 git diff。判断这些改动是否正确、安全、无明显副作用。"
        "第一行只输出 PASS 或 FAIL，后跟简短理由。\n\n" + diff[:8000]
    )
    code, out, _ = _headless(run_id, engine, prompt, cwd, REVIEW_TIMEOUT)
    if code != 0:
        return "unknown", "(复审未运行)"
    # headless 是结构化输出(json_out=True)，原始流里还夹着被回显的用户提示词。
    # 直接正则会先撞上提示词里的「PASS 或 FAIL」，把 FAIL 的复审读成 PASS。
    out = assistant_text(engine, out) or out
    up = out.upper()
    # 锚定行首优先(复审结论), 回退全局; 避免 diff 内 PASS/FAIL 字样或复述指令污染结论
    m = re.search(r"^\s*(PASS|FAIL)", up, re.M) or re.search(r"\b(PASS|FAIL)\b", up)
    if not m:
        return "unknown", "(复审无明确结论)"
    return ("pass" if m.group(1) == "PASS" else "fail"), out.strip()[:200]


def _round_closed(verify_result: str, review_verdict: str) -> bool:
    """本轮修复是否可判为闭合(标 fixed)：验证未失败 且 复审明确 PASS。

    无验证命令(skip)时由复审 PASS 提供唯一阳性信号；复审 unknown/fail 不得闭合。
    """
    return verify_result != "fail" and review_verdict == "pass"


def _loop(run_id, project, max_iter, fix_engine, review_engine, budget, scope, scope_arg) -> None:
    stop = _stops[run_id]
    cwd = project["path"]
    try:
        if _git(cwd, "rev-parse", "--is-inside-work-tree")[0] != 0:
            _finish(run_id, "failed", "目标不是 git 仓库")
            return
        # 工作区必须干净: 否则 git add -A 会把你未提交的改动扫进自愈提交, 破坏隔离
        if _git(cwd, "status", "--porcelain")[1].strip():
            _finish(run_id, "failed", "工作区有未提交改动，请先提交或暂存(git stash)后再跑自愈")
            return
        branch = f"bosun/autopilot-{int(time.time())}"
        if _git(cwd, "checkout", "-b", branch)[0] != 0:
            _finish(run_id, "failed", f"创建分支失败(可能工作区不干净): {branch}")
            return
        db.execute("UPDATE autopilot_run SET branch=? WHERE id=?", (branch, run_id))
        _log(run_id, f"创建隔离分支 {branch}")

        prev_sig = None
        for i in range(1, max_iter + 1):
            if stop.is_set():
                _finish(run_id, "stopped", f"用户停止(第{i}轮前)")
                return
            if _over_budget(run_id, budget):
                _finish(run_id, "stopped", f"token 预算耗尽(上限 {budget})，停止")
                return
            db.execute("UPDATE autopilot_run SET iteration=? WHERE id=?", (i, run_id))
            events.emit("autopilot.update", {"run_id": run_id, "iteration": i})
            _log(run_id, f"=== 第 {i}/{max_iter} 轮：审/查(范围={scope}) ===")
            t0 = time.time()
            discovery.analyze(project["id"], origin="autopilot", scope=scope, scope_arg=scope_arg)
            findings = db.query(
                "SELECT * FROM finding WHERE project_id=? AND status='open'", (project["id"],)
            )
            if not findings:
                _span(run_id, i, "analyze", "问题清零", "ok", t0, time.time())
                _finish(run_id, "done", f"第 {i} 轮问题清零 ✓")
                return
            _span(run_id, i, "analyze", f"发现 {len(findings)} 个问题", "warn", t0, time.time())
            sig = tuple(sorted((f["source"], f["title"]) for f in findings))
            if sig == prev_sig:
                _finish(run_id, "stopped", "与上一轮相同问题，无进展，停止")
                return
            prev_sig = sig
            _log(run_id, f"发现 {len(findings)} 个问题，用 {fix_engine} 修复")

            fx0, fix_tok, fix_fail = time.time(), 0, 0
            attempted_ids = []  # 本轮真正跑完(code==0)的修复; 只有这些才可能被判 fixed
            for f in findings:
                if stop.is_set():
                    _finish(run_id, "stopped", "用户停止(修复中)")
                    return
                if _over_budget(run_id, budget):
                    _finish(run_id, "stopped", f"token 预算耗尽(上限 {budget})，停止")
                    return
                code, _, toks = _headless(run_id, fix_engine, _repair_prompt(f), cwd, FIX_TIMEOUT)
                fix_tok += toks
                _log(run_id, f"  修复 [{f['source']}] {f['title'][:40]} → exit {code} (+{toks} tok)")
                if code == 0:
                    db.execute("UPDATE finding SET status='task_created' WHERE id=?", (f["id"],))
                    attempted_ids.append(f["id"])
                else:
                    # 进程失败(超时/崩溃, 可能一行没改): 保持 open, 下轮重试,
                    # 不移出 open 洗白健康分, 也不进 reappear→mute 的"清零"链
                    fix_fail += 1
            _span(run_id, i, "fix", f"修复 {len(findings)} 个", "fail" if fix_fail else "ok",
                  fx0, time.time(), fix_tok, f"{fix_fail} 个进程非零退出" if fix_fail else "")

            v0 = time.time()
            verify_result, detail = _verify(project)
            _span(run_id, i, "verify", "客观验证",
                  {"pass": "ok", "skip": "warn", "fail": "fail"}[verify_result], v0, time.time(), 0, detail)
            _log(run_id, f"客观验证：{verify_result}  {detail}")
            r0 = time.time()
            review_verdict, note = _cross_review(run_id, review_engine, cwd, _git(cwd, "diff", "HEAD")[1])
            _span(run_id, i, "review", f"交叉复审({review_engine})",
                  "ok" if review_verdict == "pass" else "warn", r0, time.time(), 0, note)
            _log(run_id, f"交叉复审({review_engine})：{review_verdict}  {note}")
            c0 = time.time()
            _git(cwd, "add", "-A")
            _git(cwd, "commit", "-m", f"bosun autopilot round {i}")
            _span(run_id, i, "commit", f"提交 round {i}", "ok", c0, time.time())
            if _round_closed(verify_result, review_verdict) and attempted_ids:
                db.execute(
                    f"UPDATE finding SET status='fixed' WHERE id IN ({','.join('?' * len(attempted_ids))})",
                    tuple(attempted_ids),
                )
                _log(run_id, f"本轮 {len(attempted_ids)} 个问题验证通过，标记为已修复 ✓")
            elif attempted_ids:
                # 无阳性验证信号: 回滚为 open, 不停在 task_created(否则会被 reappear→mute 洗成"清零")
                db.execute(
                    f"UPDATE finding SET status='open' WHERE id IN ({','.join('?' * len(attempted_ids))})",
                    tuple(attempted_ids),
                )
                _log(run_id, "本轮未获阳性验证/复审信号，问题保持待处理(open)")
            else:
                _log(run_id, "本轮无成功修复，问题保持待处理(open)")

        _finish(run_id, "done", f"达到最大迭代 {max_iter}，停止")
    except Exception as exc:  # noqa: BLE001
        _finish(run_id, "failed", f"异常: {exc}")


def start(
    project_id: int,
    max_iterations: int,
    fix_engine: str,
    review_engine: str,
    token_budget: int = 0,
    scope: str = "full",
    scope_arg: str | None = None,
    policy_id: int | None = None,
) -> dict:
    project = db.query_one("SELECT * FROM project WHERE id=?", (project_id,))
    if project is None:
        raise ValueError("项目不存在")
    run_id = db.execute(
        "INSERT INTO autopilot_run(project_id,status,max_iterations,fix_engine,review_engine,"
        "token_budget,scope,scope_arg,policy_id,created_at) "
        "VALUES(?, 'running', ?, ?, ?, ?, ?, ?, ?, ?)",
        (project_id, max_iterations, fix_engine, review_engine, token_budget,
         scope, scope_arg, policy_id, time.time()),
    )
    log_path = str(LOG_DIR / f"autopilot-{run_id}.log")
    db.execute("UPDATE autopilot_run SET log_path=? WHERE id=?", (log_path, run_id))
    _stops[run_id] = threading.Event()
    threading.Thread(
        target=_loop,
        args=(run_id, project, max_iterations, fix_engine, review_engine, token_budget, scope, scope_arg),
        daemon=True,
    ).start()
    return dict(db.query_one("SELECT * FROM autopilot_run WHERE id=?", (run_id,)))


def reconcile_on_startup() -> int:
    """进程重启后 daemon 线程已被杀, 残留在 running 的 run 永不落终态
    (has_active_run 恒真会让该项目自愈永久失效)。启动时把它们对账为 failed。返回处理条数。"""
    n = 0
    for r in db.query("SELECT id FROM autopilot_run WHERE status='running'"):
        if is_live(r["id"]):
            continue
        db.execute(
            "UPDATE autopilot_run SET status='failed', summary=?, ended_at=? WHERE id=?",
            ("进程重启中断", time.time(), r["id"]),
        )
        n += 1
    return n


def has_active_run(project_id: int) -> bool:
    return db.query_one(
        "SELECT id FROM autopilot_run WHERE project_id=? AND status='running'", (project_id,)
    ) is not None


def is_live(run_id: int) -> bool:
    return run_id in _stops


def stop(run_id: int) -> None:
    ev = _stops.get(run_id)
    if ev:
        ev.set()
