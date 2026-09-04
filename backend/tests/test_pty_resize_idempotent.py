"""PTY winsize 的同值短路。

多端同看一个任务时，各端在「自己正在被操作」时会反复认领 PTY（重发自己的网格）。
若 resize 无条件 setwinsize，同值请求也会给 TUI 打出 SIGWINCH，每次都是一屏重绘。
"""
import unittest
from unittest.mock import MagicMock

from app.pty_session import PtySession


def _session_with_proc():
    """绕开 start()，只装配 resize 需要的两个字段。"""
    s = PtySession.__new__(PtySession)
    s.proc = MagicMock()
    s.proc.isalive.return_value = True
    s._winsize = (30, 100)
    return s


class PtyResizeIdempotentTest(unittest.TestCase):
    def test_same_size_does_not_touch_ioctl(self):
        s = _session_with_proc()
        s.resize(30, 100)
        s.proc.setwinsize.assert_not_called()

    def test_changed_size_is_applied_and_recorded(self):
        s = _session_with_proc()
        s.resize(24, 80)
        s.proc.setwinsize.assert_called_once_with(24, 80)
        self.assertEqual(s._winsize, (24, 80))
        # 记账后，重复同值请求被短路
        s.proc.setwinsize.reset_mock()
        s.resize(24, 80)
        s.proc.setwinsize.assert_not_called()

    def test_rows_only_change_still_applies(self):
        s = _session_with_proc()
        s.resize(31, 100)
        s.proc.setwinsize.assert_called_once_with(31, 100)

    def test_failed_setwinsize_is_not_recorded(self):
        """没设成功就不记账，否则下次同值请求会被错误短路，尺寸永远修不回来。"""
        s = _session_with_proc()
        s.proc.setwinsize.side_effect = OSError("boom")
        s.resize(24, 80)
        self.assertEqual(s._winsize, (30, 100))
        s.proc.setwinsize.side_effect = None
        s.resize(24, 80)
        s.proc.setwinsize.assert_called_with(24, 80)
        self.assertEqual(s._winsize, (24, 80))

    def test_redraw_still_forces_a_repaint(self):
        """redraw 靠 rows-1 → rows 两次真实变化强制重绘，不能被短路吃掉。"""
        s = _session_with_proc()
        s.redraw(30, 100)
        self.assertEqual(
            [c.args for c in s.proc.setwinsize.call_args_list],
            [(29, 100), (30, 100)],
        )

    def test_dead_process_is_not_recorded(self):
        s = _session_with_proc()
        s.proc.isalive.return_value = False
        s.resize(24, 80)
        s.proc.setwinsize.assert_not_called()
        self.assertEqual(s._winsize, (30, 100))


if __name__ == "__main__":
    unittest.main()
