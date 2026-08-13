"""受控子任务的策略与等待（零依赖底层，router 与 scheduler 共同引用）。

agent 申请派生一个跑在别的引擎上的任务（典型用途：让另一个模型交叉复审自己的改动），
由调度器统一派发，**同步返回结论**——对 agent 而言等价于直接跑 `codex exec`，
迁移成本接近零；这是它会被真正用起来的前提（异步 + 轮询比直接跑 CLI 更麻烦，
agent 会绕开不用）。

子任务**不占并发槽**：今天 agent 自己在终端里跑 codex 时，那个子进程本来就不占槽、
不受 max_concurrent 约束、也不计入配额路由——超发是现状，不是这里引入的新问题。
换来的是看板可见、独立日志、可取消、计入配额、回报归属天然正确（子任务由后端直接
派发，nesting.py 的进程链恰好是 1 层，无需给拦截开任何口子）。
代价由硬上限兜住：默认每个父任务最多 3 个子任务。
"""
from __future__ import annotations

import os
import time

from . import db

_FALSE = {"0", "false", "no", "off"}

DEFAULT_MAX_CHILDREN = 3      # 即最坏情况下的额度放大倍数，刻意取小
DEFAULT_TIMEOUT = 900.0       # 15min，与 autopilot.FIX_TIMEOUT 同量级
MAX_TIMEOUT = 3600.0          # 钳制上限：父任务不能被无限期阻塞
_POLL_INTERVAL = 0.5

# 子任务「已出结论」的状态。
# waiting_input 单独处理（见 _finished）：它有两种来源，只有一种代表出了结论——
#   已出结论：agent 按收尾约定回报 done/failed 后置的 waiting_input（等人核对）
#   仍在干活：SDK 会话请求工具授权、或一轮结束等下一条输入时也置 waiting_input
#             （sdk_session.py 的 _ask_permission / 回合结束分支）
# 把后者当成「已出结论」会让父任务拿到一个空结论就往下走，而子任务其实卡在
# 授权提示上没人应答。判据取 report_result 是否落库——那是 agent 的权威回调。
_TERMINAL_STATUSES = {"done", "failed", "cancelled", "interrupted"}
FINISHED_STATUSES = _TERMINAL_STATUSES | {"waiting_input"}


def _finished(status: str, report_result: str | None) -> bool:
    if status in _TERMINAL_STATUSES:
        return True
    return status == "waiting_input" and bool(report_result)


def enabled() -> bool:
    """子任务总开关（默认开）。BOSUN_SUBTASK=0 关闭。"""
    return os.environ.get("BOSUN_SUBTASK", "1").strip().lower() not in _FALSE


def max_children() -> int:
    """单个父任务最多可派生的子任务数（默认 3）。"""
    try:
        return max(0, int(os.environ.get("BOSUN_SUBTASK_MAX", DEFAULT_MAX_CHILDREN)))
    except ValueError:
        return DEFAULT_MAX_CHILDREN


def default_timeout() -> float:
    try:
        return clamp_timeout(float(os.environ.get("BOSUN_SUBTASK_TIMEOUT", DEFAULT_TIMEOUT)))
    except ValueError:
        return DEFAULT_TIMEOUT


def clamp_timeout(value: float) -> float:
    """钳制超时：太小会误杀刚起步的子任务，太大会让父任务近乎永久阻塞。"""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT
    return min(max(seconds, 10.0), MAX_TIMEOUT)


def child_count(parent_id: int) -> int:
    """该父任务已派生的子任务数（含已完成的：上限管的是总放大倍数，不是并发数）。"""
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM task WHERE parent_task_id=? AND deleted=0", (parent_id,)
    )
    return int(row["n"]) if row else 0


def wait_for_result(child_id: int, timeout: float) -> dict:
    """阻塞等子任务出结论；超时则杀掉它并标记 timed_out。

    超时必须杀：否则子任务会继续烧额度，而父任务已经不再关心它的结果。
    """
    from . import scheduler  # 延迟导入：scheduler 依赖本模块，避免循环

    deadline = time.time() + timeout
    while True:
        row = db.query_one(
            "SELECT status, report_result, report_summary FROM task WHERE id=?", (child_id,)
        )
        if row is None:  # 被删了，没有结论可等
            return {"status": "cancelled", "summary": "", "timed_out": False}
        if _finished(row["status"], row["report_result"]):
            return {
                "status": row["status"],
                "result": row["report_result"],
                "summary": row["report_summary"] or "",
                "timed_out": False,
            }
        if time.time() >= deadline:
            scheduler.cancel(child_id)
            return {
                "status": "cancelled",
                "result": None,
                "summary": f"子任务超时({int(timeout)}s)未出结论，已终止",
                "timed_out": True,
            }
        time.sleep(_POLL_INTERVAL)
