import tempfile
import threading
import unittest
from pathlib import Path

from backend.app.pty_session import PtySession
from backend.app.terminal_log_compact import TerminalLogCompactor, compact_terminal_frames


def _omp_frame(rows: dict[int, bytes], title: bytes = b"t") -> bytes:
    """omp 的一次重绘：还原上一帧的自动换行 + 标题 + 隐光标关自动换行 + 逐行「定位/复位/
    清行/写字」+ 显光标。帧尾的 ?7h 实际跟在 ?25h 后面，这里归到下一帧开头，与压缩器的
    切分方式一致。"""
    body = b"".join(
        b"\x1b[%d;1H\x1b[0m\x1b[K" % row + text + b"\x1b[0m\r" for row, text in rows.items()
    )
    return (
        b"\x1b[?7h\x1b]0;" + title + b"\x07\x1b[?25l\x1b[?7l"
        + body
        + b"\x1b[47;2H\x1b[?25h"
    )


def _status(i: int) -> bytes:
    return _omp_frame({49: b"\x1b[38;2;0;180;255m spinner %d" % i}, title=b"%d" % i)


class CompactTerminalFramesTest(unittest.TestCase):
    def test_status_bar_repaints_keep_only_the_last(self):
        frames = [_status(i) for i in range(30)]
        self.assertEqual(compact_terminal_frames(b"".join(frames)), frames[-1])

    def test_body_rows_not_redrawn_later_survive(self):
        body = _omp_frame({20: b"streamed answer"})
        data = _status(1) + body + _status(2) + _status(3)
        self.assertEqual(compact_terminal_frames(data), body + _status(3))

    def test_body_row_rewritten_later_drops_the_older_frame(self):
        partial = _omp_frame({20: b"Awaiting mer"})
        whole = _omp_frame({20: b"Awaiting merge target choice"})
        data = partial + _status(1) + whole + _status(2)
        self.assertEqual(compact_terminal_frames(data), whole + _status(2))

    def test_frame_is_dropped_only_when_every_row_is_covered(self):
        two_rows = _omp_frame({20: b"a", 21: b"b"})
        only_20 = _omp_frame({20: b"c"})
        data = two_rows + only_20
        self.assertEqual(compact_terminal_frames(data), data)

    def test_scrolling_output_is_a_barrier(self):
        before = _status(1)
        scroll = b"\x1b[?25l\x1b[48;1H\r\nnew line\x1b[?25h"
        after = _status(2)
        data = before + scroll + after
        self.assertEqual(compact_terminal_frames(data), data)

    def test_plain_output_between_frames_is_a_barrier(self):
        data = _status(1) + b"plain output\r\n" + _status(2)
        self.assertEqual(compact_terminal_frames(data), data)

    def test_frames_depending_on_outer_state_are_kept(self):
        # 没关自动换行：长文本可能折到下一行，画了哪些行不确定
        wraps = b"\x1b[?25l\x1b[49;1H\x1b[0m\x1b[Kspinner\x1b[?25h"
        # 没复位 SGR：颜色依赖帧外状态
        inherits = b"\x1b[?25l\x1b[?7l\x1b[49;1H\x1b[Kspinner\x1b[?25h\x1b[?7h"
        for frame in (wraps, inherits):
            data = frame + _status(1)
            self.assertEqual(compact_terminal_frames(data), data)

    def test_writing_from_mid_row_does_not_cover_the_row(self):
        tail_only = b"\x1b[?25l\x1b[?7l\x1b[20;5H\x1b[0m\x1b[Kxyz\x1b[?25h\x1b[?7h"
        body = _omp_frame({20: b"streamed answer"})
        data = body + tail_only
        self.assertEqual(compact_terminal_frames(data), data)

    def test_real_omp_spinner_stream_keeps_last_frame_and_trailer(self):
        def frame(spin: bytes) -> bytes:
            return (
                b"\x1b]0;\xcf\x80 " + spin + b" demo_project\x07\x1b[?25l\x1b[?7l\x1b[49;1H"
                b"\x1b[0m\x1b[K\x1b[49;39m \x1b[38;2;0;180;255m" + spin + b" 17m\x1b[39m "
                b"\x1b[0m\x1b[0m\r\x1b[47;2H\x1b[?25h\x1b[?7h"
            )

        spins = [b"\xe2\xa0\x8f", b"\xe2\xa0\x8b", b"\xe2\xa0\x99", b"\xe2\xa0\xb9"]
        data = b"".join(frame(s) for s in spins)
        # 上一帧帧尾的 ?7h 随最后一帧的间隙保留，终态与原始流一致
        self.assertEqual(compact_terminal_frames(data), b"\x1b[?7h" + frame(spins[-1]))

    def test_sync_frames_with_relative_moves_are_untouched(self):
        codex = b"\x1b[?2026h\x1b[3A\x1b[2KWorking\x1b[?2026l"
        data = codex * 5
        self.assertEqual(compact_terminal_frames(data), data)


class TerminalLogCompactorStreamingTest(unittest.TestCase):
    def _sample(self) -> bytes:
        parts = []
        for i in range(40):
            parts.append(_status(i))
            if i % 7 == 0:
                parts.append(_omp_frame({20 + i % 3: b"\xe4\xbd\xa0\xe5\xa5\xbd answer %d" % i}))
            if i % 11 == 0:
                parts.append(b"\x1b[?25l\x1b[48;1H\r\nscrolled %d\x1b[?25h" % i)
        return b"".join(parts)

    def test_chunked_feed_matches_one_shot_for_any_split(self):
        data = self._sample()
        expected = compact_terminal_frames(data)
        for size in (1, 3, 17, 64, 4096):
            compactor = TerminalLogCompactor(max_pending_age=float("inf"))
            out = [compactor.feed(data[i : i + size], now=0.0) for i in range(0, len(data), size)]
            out.append(compactor.flush())
            self.assertEqual(b"".join(out), expected, size)

    def test_pending_frames_are_released_after_max_age(self):
        compactor = TerminalLogCompactor(max_pending_age=2.0)
        self.assertEqual(compactor.feed(_status(1), now=0.0), b"")
        self.assertEqual(compactor.flush_if_stale(now=1.0), b"")
        self.assertEqual(compactor.flush_if_stale(now=2.5), _status(1))
        self.assertEqual(compactor.flush(), b"")

    def test_flush_emits_incomplete_frame_bytes(self):
        compactor = TerminalLogCompactor()
        half = _status(1)[:20]
        self.assertEqual(compactor.feed(half, now=0.0), b"")
        self.assertEqual(compactor.flush(), half)


class PtySessionLogCompactionTest(unittest.TestCase):
    def _session(self, log_path: Path) -> PtySession:
        s = PtySession.__new__(PtySession)
        s.log_path = str(log_path)
        s._log_fh = open(log_path, "ab", buffering=0)
        s._log_compactor = TerminalLogCompactor()
        s._log_lock = threading.Lock()
        return s

    def test_backlog_includes_frames_still_held_by_the_compactor(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "task.log"
            s = self._session(log)
            try:
                for i in range(10):
                    s._write_log(s._log_compactor.feed, _status(i))
                backlog = s.read_backlog()
                self.assertIn(b"spinner 9", backlog.data)
                self.assertNotIn(b"spinner 3", log.read_bytes())
            finally:
                s._log_fh.close()


if __name__ == "__main__":
    unittest.main()
