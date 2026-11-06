import tempfile
import unittest
from pathlib import Path

from backend.app.terminal_modes import TerminalModes
from backend.app.pty_session import read_terminal_backlog


class TerminalModesTest(unittest.TestCase):
    def test_split_sequence_and_reset(self):
        state = TerminalModes()
        state.feed(b"\x1b[?104")
        state.feed(b"9;1000;1006h")
        self.assertEqual(state.prefix(), b"\x1b[?1049h\x1b[?1000h\x1b[?1006h")
        state.feed(b"\x1bc")
        self.assertEqual(state.prefix(), b"")

    def test_mouse_updates_preserve_order(self):
        state = TerminalModes()
        state.feed(b"\x1b[?1000h\x1b[?1003h\x1b[?1003l\x1b[?1000h")
        self.assertEqual(state.prefix(), b"\x1b[?1003l\x1b[?1000h")

    def test_modes_survive_both_scan_and_replay_truncation(self):
        for scan_bytes in (1024, 65536):
            with self.subTest(scan_bytes=scan_bytes), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "task.log"
                path.write_bytes(b"\x1b[?1049h\x1b[?1003h\x1b[?1006h" + b"\x1b[1;1Hscreen" * 1000)
                result = read_terminal_backlog(str(path), max_bytes=512, scan_bytes=scan_bytes)
                self.assertTrue(result.truncated)
                self.assertTrue(result.data.startswith(b"\x1b[?1049h\x1b[?1003h\x1b[?1006h"))
                self.assertLessEqual(len(result.data), 512)

    def test_mode_closed_in_discarded_middle_is_not_reenabled(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "task.log"
            path.write_bytes(b"\x1b[?1003h" + b"x" * 2048 + b"\x1b[?1003l" + b"\x1b[1;1Hdone" * 500)
            result = read_terminal_backlog(str(path), max_bytes=512, scan_bytes=1024)
            self.assertTrue(result.data.startswith(b"\x1b[?1003l"))


if __name__ == "__main__":
    unittest.main()
