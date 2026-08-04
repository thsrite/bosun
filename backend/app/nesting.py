"""识别「嵌套 agent 冒用父任务身份回报」。

BOSUN_TASK_ID 是通过环境变量注入给 agent 的，agent 自己再拉起一个 agent
(例如让 codex 跑一轮第二意见审查)时会连同这个变量一起继承下去；子 agent 若也装了
bosun-report skill，就会拿着父任务的 id 回报状态——实际发生过一次「codex 审查完
把父任务报成 done」。

判定放在后端而不是 skill 脚本里，有两个决定性理由：
1. 子 agent 可能跑在沙箱里读不到进程表，让它自证「我不是嵌套的」本身就不可靠；
2. 后端知道自己 spawn 的 agent 真实 pid，不必靠进程名去猜哪个才是顶层 agent。

于是判据变得很干净：从回报者往上走到 agent_pid，**中间只要还夹着一个 agent 进程，
就是嵌套**。顶层 agent 自己不参与匹配(它就是 agent_pid)，所以进程名只用于识别
「多出来的那个」。

拿不准一律放行：进程已退出、链路断了、拿不到 agent pid——宁可多收一次回报，
也不能把正常任务的回报拒掉(那会让 Bosun 退回靠终端输出猜状态)。
"""
from __future__ import annotations

import subprocess

# Bosun 认识的 agent 可执行文件名(实测 ps comm：Claude Code 是 claude，不是 node)。
# 只用来识别「顶层 agent 之下多出来的那个 agent」。
AGENT_COMMANDS = {"claude", "codex", "omp"}

_MAX_DEPTH = 64  # 父链兜底深度，防坏数据下的长循环


def normalize_comm(comm: str) -> str:
    """ps 的 comm 可能是全路径，登录 shell 还会带前导 '-'。"""
    name = (comm or "").strip().rsplit("/", 1)[-1]
    return name[1:] if name.startswith("-") else name


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


def agents_between(
    reporter_pid: int, agent_pid: int, table: dict[int, tuple[int, str]]
) -> int | None:
    """数回报者与 agent_pid 之间夹着几个 agent 进程。走不到 agent_pid 返回 None。"""
    if reporter_pid == agent_pid:
        return 0
    count = 0
    current = reporter_pid
    for _ in range(_MAX_DEPTH):
        entry = table.get(current)
        if entry is None:
            return None
        parent = entry[0]
        if parent == agent_pid:
            return count
        if parent in (0, current):
            return None  # 走到 init 都没遇到 agent：链路对不上
        parent_entry = table.get(parent)
        if parent_entry is None:
            return None
        if normalize_comm(parent_entry[1]) in AGENT_COMMANDS:
            count += 1
        current = parent
    return None


def is_nested_report(
    reporter_pid: int | None,
    agent_pid: int | None,
    table: dict[int, tuple[int, str]] | None = None,
) -> bool:
    """回报是否来自嵌套 agent。任何拿不准的情况都返回 False(放行)。"""
    if not reporter_pid or not agent_pid:
        return False
    count = agents_between(reporter_pid, agent_pid, table if table is not None else read_process_table())
    return bool(count)
