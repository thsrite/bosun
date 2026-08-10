"""识别「嵌套 agent 冒用父任务身份回报」。

BOSUN_TASK_ID 是通过环境变量注入给 agent 的，agent 自己再拉起一个 agent
(例如让 codex 跑一轮第二意见审查)时会连同这个变量一起继承下去；子 agent 若也装了
收尾回报约定，就会拿着父任务的 id 回报状态——实际发生过一次「codex 审查完
把父任务报成 done」。

判定放在后端而不是 skill 脚本里：子 agent 可能跑在沙箱里读不到进程表，
让它自证「我不是嵌套的」本身就不可靠；后端不受这个限制。

边界取**后端进程自己的 pid**，判据是「从回报者到后端之间夹了几个 agent」：

    正常(PTY)        report.sh ← shell ← claude ← 后端                1 个 → 放行
    正常(script 包)  report.sh ← shell ← claude ← script ← 后端       1 个 → 放行
    正常(SDK)        report.sh ← shell ← claude ← 后端                1 个 → 放行
    嵌套             report.sh ← shell ← codex ← shell ← claude ← 后端 2 个 → 拒绝

不去精确定位「哪个进程才是顶层 agent」是刻意的：SDK 走 subprocess 传输另起
claude 进程、开了 BOSUN_SCRIPT_LOG 时 spawn 的是 script 包装——凡是想钉死某个
pid 的做法都会在这些路径上把正常回报误判掉。后端 pid 则始终可靠。

相邻的两个 agent 也照常各计一层：agent 不经 shell 直接 Popen 子 agent 同样是嵌套。

拿不准一律放行：进程已退出、链路断了、进程表读不到——宁可多收一次回报，
也不能把正常任务的回报拒掉(那会让 Bosun 退回靠终端输出猜状态)。
"""
from __future__ import annotations

import os
import subprocess

# Bosun 认识的 agent 可执行文件名(实测 ps comm：Claude Code 是 claude，不是 node)。
# 只用来识别「顶层 agent 之下多出来的那个 agent」。
AGENT_COMMANDS = {"claude", "codex", "omp", "kimi"}

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


# 正常链路允许出现的 agent 层数：Bosun 自己派发的那个 agent 占一层。
MAX_AGENT_LAYERS = 1


def agents_between(
    reporter_pid: int, boundary_pid: int, table: dict[int, tuple[int, str]]
) -> int | None:
    """数回报者与 boundary_pid 之间夹着几个 agent 进程。走不到边界返回 None。"""
    if reporter_pid == boundary_pid:
        return 0
    count = 0
    current = reporter_pid
    for _ in range(_MAX_DEPTH):
        entry = table.get(current)
        if entry is None:
            return None
        parent = entry[0]
        if parent == boundary_pid:
            return count
        if parent in (0, current):
            return None  # 走到 init 都没遇到后端：链路对不上
        parent_entry = table.get(parent)
        if parent_entry is None:
            return None
        if normalize_comm(parent_entry[1]) in AGENT_COMMANDS:
            count += 1
        current = parent
    return None


def is_nested_report(
    reporter_pid: int | None,
    boundary_pid: int | None = None,
    table: dict[int, tuple[int, str]] | None = None,
) -> bool:
    """回报是否来自嵌套 agent。任何拿不准的情况都返回 False(放行)。"""
    if not reporter_pid:
        return False
    boundary = boundary_pid or os.getpid()
    count = agents_between(
        reporter_pid, boundary, table if table is not None else read_process_table()
    )
    if count is None:
        return False
    return count > MAX_AGENT_LAYERS
