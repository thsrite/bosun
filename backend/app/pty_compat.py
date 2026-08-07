"""平台 PTY 适配层：POSIX 用 ptyprocess，Windows 用 pywinpty（ConPTY）。

会话层统一从这里拿 PtyProcess。接口对齐 ptyprocess 的语义：
read/write 走 bytes，EOF 抛 EOFError，spawn(argv, cwd, env, dimensions)。
"""
from __future__ import annotations

import sys

IS_WINDOWS = sys.platform == "win32"

if not IS_WINDOWS:
    from ptyprocess import PtyProcess  # noqa: F401  # re-export，POSIX 直接用原实现
else:
    import shutil

    from winpty import PtyProcess as _WinPtyProcess

    def _normalize_argv(argv: list[str]) -> list[str]:
        """npm 全局安装的 CLI 在 Windows 上是 .cmd/.bat shim，CreateProcess
        不能直接执行，须经 cmd /c 包一层。"""
        resolved = shutil.which(argv[0]) or argv[0]
        if resolved.lower().endswith((".cmd", ".bat")):
            return ["cmd.exe", "/c", resolved, *argv[1:]]
        return [resolved, *argv[1:]]

    class PtyProcess:
        """包装 pywinpty：pywinpty 的 read/write 是 str 接口（内部已按 UTF-8
        与 ConPTY 互转），这里统一回 bytes，与 ptyprocess 对齐。"""

        def __init__(self, impl: _WinPtyProcess) -> None:
            self._impl = impl

        @classmethod
        def spawn(
            cls,
            argv: list[str],
            cwd: str | None = None,
            env: dict | None = None,
            dimensions: tuple[int, int] = (24, 80),
        ) -> "PtyProcess":
            clean_env = {str(k): str(v) for k, v in (env or {}).items()}
            impl = _WinPtyProcess.spawn(
                _normalize_argv(list(argv)), cwd=cwd, env=clean_env, dimensions=dimensions
            )
            return cls(impl)

        def read(self, size: int = 1024) -> bytes:
            return self._impl.read(size).encode("utf-8", errors="replace")

        def write(self, data: bytes) -> int:
            return self._impl.write(data.decode("utf-8", errors="replace"))

        def setwinsize(self, rows: int, cols: int) -> None:
            self._impl.setwinsize(rows, cols)

        def isalive(self) -> bool:
            return self._impl.isalive()

        def wait(self) -> int | None:
            return self._impl.wait()

        @property
        def exitstatus(self) -> int | None:
            return self._impl.exitstatus

        @property
        def pid(self) -> int | None:
            return getattr(self._impl, "pid", None)

        def terminate(self, force: bool = False) -> bool:
            return self._impl.terminate(force=force)
