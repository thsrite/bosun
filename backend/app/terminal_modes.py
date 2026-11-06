"""为截尾回放保留终端模式；历史正文仍由原来的回放预算控制。"""
from __future__ import annotations

import re

_MODES = {6, 7, 25, 47, 1047, 1049, 1000, 1002, 1003, 1004, 1005, 1006, 1015, 2004}
_TOKEN = re.compile(rb"\x1b\[\?([0-9;]+)([hl])|\x1bc")
_INCOMPLETE = re.compile(rb"\x1b(?:\[\??[0-9;]*)?\Z")


class TerminalModes:
    def __init__(self):
        self._values: dict[int, bytes] = {}
        self._carry = b""

    def feed(self, data: bytes) -> None:
        data = self._carry + data
        self._carry = b""
        for match in _TOKEN.finditer(data):
            if match.group(1) is None:
                self._values.clear()  # RIS：恢复默认模式
                continue
            for raw in match.group(1).split(b";"):
                if not raw or len(raw) > 5:
                    continue
                mode = int(raw)
                if mode in _MODES:
                    # 鼠标模式互斥；按最后一次设置的顺序恢复，不能只按编号排序。
                    self._values.pop(mode, None)
                    self._values[mode] = match.group(2)
        tail = _INCOMPLETE.search(data)
        if tail and len(data) - tail.start() <= 128:
            self._carry = data[tail.start():]

    def prefix(self) -> bytes:
        return b"".join(b"\x1b[?%d" % mode + value for mode, value in self._values.items())
