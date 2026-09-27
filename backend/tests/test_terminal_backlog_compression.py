import tempfile
import unittest
from pathlib import Path

from backend.app.pty_session import (
    MAX_TERMINAL_BACKLOG_BYTES,
    compress_terminal_repaints,
    read_terminal_backlog,
)


def _pure_frame(spinner: bytes) -> bytes:
    """可安全合并的完整状态栏：关闭换行、复位样式并从首列清整行。"""
    body = b"".join(
        b"\x1b[%d;1H\x1b[0m\x1b[49m\x1b[K" % row for row in range(52, 59)
    )
    return b"\x1b[?2026h\x1b[?7l" + body + b"\x1b[53;1H" + spinner + b"\x1b[?7h\x1b[?2026l"


def _content_frame(text: bytes) -> bytes:
    """插入一行转录正文的帧：设滚动区 + 换行滚动 + 打印内容。"""
    return (
        b"\x1b[?2026h\x1b[1;51r\x1b[51;1H\r\n\x1b[K" + text + b"\x1b[?2026l"
    )


class CompressTerminalRepaintsTest(unittest.TestCase):
    def test_run_of_pure_repaint_frames_keeps_only_the_last(self):
        frames = [_pure_frame(b"Work%d" % i) for i in range(50)]
        compressed = compress_terminal_repaints(b"".join(frames))
        self.assertEqual(compressed, frames[-1])

    def test_content_frames_survive_in_order(self):
        data = (
            _content_frame(b"line one")
            + _pure_frame(b"W")
            + _pure_frame(b"Wo")
            + _content_frame(b"line two")
            + _pure_frame(b"Working")
        )
        compressed = compress_terminal_repaints(data)
        self.assertIn(b"line one", compressed)
        self.assertIn(b"line two", compressed)
        self.assertLess(compressed.find(b"line one"), compressed.find(b"line two"))
        # 每段纯重绘只留最后一帧
        self.assertNotIn(b"HW\x1b", compressed)
        self.assertIn(b"Working", compressed)

    def test_window_title_updates_between_pure_frames_are_dropped_with_them(self):
        data = (
            _pure_frame(b"W")
            + b"\x1b]0;\xe2\xa0\xa7 task\x07"
            + _pure_frame(b"Wo")
            + _pure_frame(b"Wor")
        )
        compressed = compress_terminal_repaints(data)
        self.assertEqual(compressed, _pure_frame(b"Wor"))

    def test_no_newline_does_not_mean_the_previous_frame_was_overwritten(self):
        # 清正文和更新状态栏都没有换行，旧逻辑却会把清行动作删掉，留下旧字。
        clear_body = b"\x1b[?2026h\x1b[?7l\x1b[20;1H\x1b[0m\x1b[2K\x1b[?2026l"
        data = clear_body + _pure_frame(b"Working")
        self.assertEqual(compress_terminal_repaints(data), data)

    def test_codex_incremental_text_and_relative_cursor_moves_survive(self):
        for body in (b"\x1b[20;8Hupdated", b"\x1b[3A\x1b[2Kupdated", b"\x1b[2S"):
            with self.subTest(body=body):
                delta = b"\x1b[?2026h" + body + b"\x1b[?2026l"
                data = delta + _pure_frame(b"Working")
                self.assertEqual(compress_terminal_repaints(data), data)

    def test_codex_screen_diff_followed_by_cursor_only_frame_survives_replay(self):
        # #1619 的序列形态：绝对定位改正文，随后一帧仅复位颜色/定位/显光标。
        # 两帧都无换行，后者并没有覆盖前者的任何文字。
        body = b"\x1b[?2026h\x1b[?25l\x1b[20;1Hanswer\x1b[K\x1b[?2026l"
        cursor = b"\x1b[?2026h\x1b[39m\x1b[49m\x1b[0m\x1b[27;3H\x1b[?25h\x1b[?2026l"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "task.log"
            path.write_bytes(body + cursor)
            self.assertEqual(read_terminal_backlog(str(path)).data, body + cursor)

    def test_later_partial_row_clear_does_not_cover_earlier_text(self):
        first = b"\x1b[?2026h\x1b[?7l\x1b[20;1H\x1b[0m\x1b[Kanswer\x1b[?2026l"
        tail = b"\x1b[?2026h\x1b[?7l\x1b[20;5H\x1b[0m\x1b[Kx\x1b[?2026l"
        self.assertEqual(compress_terminal_repaints(first + tail), first + tail)

    def test_non_title_bytes_between_frames_are_preserved(self):
        data = _pure_frame(b"W") + b"plain output\r\n" + _pure_frame(b"Wo")
        compressed = compress_terminal_repaints(data)
        self.assertIn(b"plain output\r\n", compressed)
        # 保留字节的相对顺序：前帧在 gap 前，后帧在 gap 后
        self.assertLess(
            compressed.find(b"\x1b[?2026h"), compressed.find(b"plain output")
        )
        self.assertIn(_pure_frame(b"Wo"), compressed)

    def test_unclosed_trailing_frame_is_left_untouched(self):
        data = _content_frame(b"line") + b"\x1b[?2026h\x1b[52;2H\x1b[Kpartial"
        compressed = compress_terminal_repaints(data)
        self.assertTrue(compressed.endswith(b"partial"))

    def test_stream_without_sync_frames_is_unchanged(self):
        data = b"hello\r\nworld\r\n" + b"\x1b[1;32mgreen\x1b[0m"
        self.assertEqual(compress_terminal_repaints(data), data)


class ReadTerminalBacklogCompressionTest(unittest.TestCase):
    def test_transcript_buried_under_spinner_churn_is_still_replayed(self):
        """回归：45MB 日志末尾 2MB 全被 spinner 重绘占满 → 重连黑屏只剩状态条。

        转录正文哪怕被 >2MB 的纯重绘噪音压在后面，压缩后也必须回放出来。
        """
        transcript = _content_frame(b"real transcript line")
        churn_frame = _pure_frame(b"Working")
        churn = churn_frame * (MAX_TERMINAL_BACKLOG_BYTES // len(churn_frame) + 100)
        self.assertGreater(len(churn), MAX_TERMINAL_BACKLOG_BYTES)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "task.log"
            path.write_bytes(transcript + churn)

            backlog = read_terminal_backlog(str(path))

        self.assertIn(b"real transcript line", backlog.data)
        self.assertIn(b"Working", backlog.data)
        self.assertLessEqual(len(backlog.data), MAX_TERMINAL_BACKLOG_BYTES)

    def test_truncated_flag_reports_dropped_content(self):
        # 小日志：全量回放，不截断
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "task.log"
            path.write_bytes(b"tiny log\r\n")
            self.assertFalse(read_terminal_backlog(str(path)).truncated)

            # 压缩后仍超出预算：截断标记必须为真
            unique_lines = b"".join(
                _content_frame(b"line %08d" % i) for i in range(80000)
            )
            self.assertGreater(len(unique_lines), MAX_TERMINAL_BACKLOG_BYTES)
            path.write_bytes(unique_lines)
            result = read_terminal_backlog(str(path))
            self.assertTrue(result.truncated)
            self.assertLessEqual(len(result.data), MAX_TERMINAL_BACKLOG_BYTES)


if __name__ == "__main__":
    unittest.main()
