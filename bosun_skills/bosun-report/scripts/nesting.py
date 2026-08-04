"""判断当前回报是否来自「嵌套 agent」，避免子 agent 把父任务标成已完成。

Bosun 把 BOSUN_TASK_ID 注入给它派发的 agent 进程。agent 自己再拉起一个 agent
(例如让 codex 跑一轮第二意见审查)时，这个变量会随环境继承下去；子 agent 若也装了
bosun-report skill，就会拿着父任务的 id 回报状态——实际发生过一次「codex 审查完
把父任务报成 done」。

判据是父进程链的形状，而不是进程名：
从当前进程往上走到 Bosun 后端进程(BOSUN_BACKEND_PID)，统计中间的非 shell 进程。

    正常(CLI 引擎)   report.sh ← bash ← claude ← 后端          非 shell 1 层
    正常(script 包)  report.sh ← bash ← claude ← script ← 后端  非 shell 2 层(script 计入 shell)
    正常(SDK 直跑)   report.sh ← bash ← 后端                    非 shell 0 层
    嵌套             report.sh ← zsh ← codex ← zsh ← claude ← 后端  非 shell 2 层

所以「≥2 层非 shell」才判定为嵌套。按进程名认 agent 不可靠(claude 是 node、
omp 是 bun)，按链路形状判断才稳。

拿不准一律放行：链路断了、后端 pid 没给、ps 读不到——宁可多报一次，
也不能把正常任务的回报吞掉(那会让 Bosun 退回靠终端输出猜状态)。
"""
from __future__ import annotations

import subprocess
import sys

# 这些进程只是「传话筒」，不算独立的 agent 层级。
# script 是 Bosun 自己可选的终端录制包装(BOSUN_SCRIPT_LOG)，同样不算。
SHELL_COMMANDS = {
    "sh", "bash", "zsh", "dash", "ksh", "csh", "tcsh", "fish",
    "script", "env", "login", "su", "nohup", "timeout", "stdbuf",
}

# 允许的非 shell 层数：agent 自己占一层。超过说明中间还夹着别的 agent。
MAX_AGENT_LAYERS = 1


def normalize_comm(comm: str) -> str:
    """ps 的 comm 可能是全路径，登录 shell 还会带前导 '-'。"""
    name = (comm or "").strip().rsplit("/", 1)[-1]
    return name[1:] if name.startswith("-") else name


def count_foreign_ancestors(
    pid: int, stop_pid: int, table: dict[int, tuple[int, str]]
) -> int | None:
    """数 pid 与 stop_pid 之间的非 shell 祖先。走不到 stop_pid 返回 None(判为拿不准)。"""
    if pid == stop_pid:
        return 0
    seen: set[int] = set()
    count = 0
    current = pid
    while True:
        entry = table.get(current)
        if entry is None:
            return None
        parent, _ = entry
        if parent in (0, current) or parent in seen:
            return None  # 到 init 还没遇到后端，或父链成环
        if parent == stop_pid:
            return count
        seen.add(parent)
        parent_entry = table.get(parent)
        if parent_entry is None:
            return None
        if normalize_comm(parent_entry[1]) not in SHELL_COMMANDS:
            count += 1
        current = parent


def is_nested(pid: int, stop_pid: int, table: dict[int, tuple[int, str]]) -> bool:
    """只有能确凿走通父链、且中间夹了不止一层非 shell 时才判定为嵌套。"""
    count = count_foreign_ancestors(pid, stop_pid, table)
    if count is None:
        return False  # 拿不准 → 放行
    return count > MAX_AGENT_LAYERS


def read_process_table() -> dict[int, tuple[int, str]]:
    """pid -> (ppid, comm)。读不到就返回空表，调用方按「拿不准」处理。"""
    try:
        out = subprocess.run(
            ["ps", "-Ao", "pid=,ppid=,comm="],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    table: dict[int, tuple[int, str]] = {}
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            table[int(parts[0])] = (int(parts[1]), parts[2])
        except ValueError:
            continue
    return table


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("ok")
        return 0
    try:
        pid, stop_pid = int(argv[1]), int(argv[2])
    except ValueError:
        print("ok")
        return 0
    print("nested" if is_nested(pid, stop_pid, read_process_table()) else "ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
