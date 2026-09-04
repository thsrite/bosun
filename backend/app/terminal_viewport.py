"""多端同看一个任务时的 PTY 窗口尺寸仲裁。

一个任务只有一个子进程、一个 PTY，PTY 只有一份 winsize；多个 WebSocket 只是订阅
同一份已经排好版的字节流，做不到「每个连接各自一个尺寸」。谁都能直接改 winsize 的
结果是：手机端一连上来就把 PTY 压到手机宽度，电脑端看到的全屏 TUI 跟着被重排。

这里按连接记录各自期望的尺寸，PTY 取所有活跃连接的最大值：大屏端永远按自己的宽度
渲染、不被小屏端压窄；小屏端拿仲裁结果回来按同一网格渲染并整体缩放查看。小屏端单独
连接时最大值就是它自己，体验不受影响。
"""
from __future__ import annotations

# xterm 与 PTY 的合理上限，超出即视为畸形控制帧（而非某个超宽显示器）
MAX_TERMINAL_DIMENSION = 1000

ConnKey = object
Size = tuple[int, int]


def _validate(rows: int, cols: int) -> Size:
    if not (1 <= rows <= MAX_TERMINAL_DIMENSION and 1 <= cols <= MAX_TERMINAL_DIMENSION):
        raise ValueError(f"invalid terminal size: {rows}x{cols}")
    return rows, cols


class ViewportRegistry:
    """task_id → {连接 → 该连接期望的 (rows, cols)}，仲裁结果取各维度最大值。"""

    def __init__(self) -> None:
        self._by_task: dict[int, dict[ConnKey, Size]] = {}

    def set(self, task_id: int, conn: ConnKey, rows: int, cols: int) -> Size:
        """记录一个连接的期望尺寸，返回仲裁后的 (rows, cols)。尺寸非法时抛 ValueError。"""
        size = _validate(rows, cols)  # 先校验，非法值不得污染已有状态
        self._by_task.setdefault(task_id, {})[conn] = size
        effective = self.effective(task_id)
        assert effective is not None  # 刚写入过，必然非空
        return effective

    def drop(self, task_id: int, conn: ConnKey) -> Size | None:
        """连接断开；返回剩余连接的仲裁结果，已无连接则返回 None。"""
        sizes = self._by_task.get(task_id)
        if sizes is None:
            return None
        sizes.pop(conn, None)
        if not sizes:
            self._by_task.pop(task_id, None)
            return None
        return self.effective(task_id)

    def effective(self, task_id: int) -> Size | None:
        sizes = self._by_task.get(task_id)
        if not sizes:
            return None
        return max(rows for rows, _ in sizes.values()), max(cols for _, cols in sizes.values())
