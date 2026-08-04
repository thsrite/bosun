"""判断当前回报是否来自「嵌套 agent」，避免子 agent 把父任务标成已完成。

Bosun 把 BOSUN_TASK_ID 注入给它派发的 agent 进程。agent 自己再拉起一个 agent
(例如让 codex 跑一轮第二意见审查)时，这个变量会随环境继承下去；子 agent 若也装了
bosun-report skill，就会拿着父任务的 id 回报状态——实际发生过一次「codex 审查完
把父任务报成 done」。

判据：从当前进程沿父链走到 Bosun 后端进程(BOSUN_BACKEND_PID)，数链上出现了几个
**已知 agent 进程**。正常链路只有顶层 agent 一个；出现第二个就说明中间又起了一个。

    正常   report.sh ← zsh ← claude ← 后端                 命中 1 → 放行
    嵌套   report.sh ← zsh ← codex ← zsh ← claude ← 后端    命中 2 → 拦下

早先的实现是数「非 shell 层数」，但实测发现 agent 与 report.sh 之间只要多出任何一层
包装进程(哪怕只是个 python)，正常回报就会被误判吞掉——这个方向的代价远比漏判大，
所以改成只认已知 agent 名，宁可漏判也不误伤。

拿不准一律放行：链路断了、后端 pid 没给、ps 读不到——宁可多报一次，
也不能把正常任务的回报吞掉(那会让 Bosun 退回靠终端输出猜状态)。
"""
from __future__ import annotations

import subprocess
import sys

# 已知的 agent 可执行文件名(实测 ps comm：Claude Code 是 claude，不是 node)。
# 只认确定的名字：omp 可能以 bun 出现，但把 bun/node 这类通用运行时算进来会误伤
# 普通包装进程，宁可漏判一次嵌套，也不能把正常任务的回报吞掉。
AGENT_COMMANDS = {"claude", "codex", "omp"}

# 链上允许出现的 agent 数量：顶层 agent 自己占一个。
MAX_AGENT_LAYERS = 1


def normalize_comm(comm: str) -> str:
    """ps 的 comm 可能是全路径，登录 shell 还会带前导 '-'。"""
    name = (comm or "").strip().rsplit("/", 1)[-1]
    return name[1:] if name.startswith("-") else name


def count_agent_ancestors(
    pid: int, stop_pid: int, table: dict[int, tuple[int, str]]
) -> int | None:
    """数 pid 与 stop_pid 之间的已知 agent 祖先。走不到 stop_pid 返回 None(判为拿不准)。"""
    if pid == stop_pid:
        return 0
    seen: set[int] = set()
    count = 0
    previous_was_agent = False
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
        is_agent = normalize_comm(parent_entry[1]) in AGENT_COMMANDS
        # 相邻的两个 agent 进程算作同一层：codex 用 npm 安装时是「启动器 + 本体」
        # 直接父子相连，那是一个 agent 的两个进程，不是嵌套。真正的嵌套之间隔着
        # agent 自己起的 shell，不会直接相邻。
        if is_agent and not previous_was_agent:
            count += 1
        previous_was_agent = is_agent
        current = parent


def is_nested(pid: int, stop_pid: int, table: dict[int, tuple[int, str]]) -> bool:
    """只有能确凿走通父链、且链上出现不止一个已知 agent 时才判定为嵌套。"""
    count = count_agent_ancestors(pid, stop_pid, table)
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
