"""Control the launchd-managed Bosun backend process."""
from __future__ import annotations

import os
import re
import subprocess


BACKEND_LABEL = "com.thsrite.bosun.backend"
_LAUNCHCTL = "/bin/launchctl"


class BackendRestartUnavailable(RuntimeError):
    """Raised when the current backend cannot be safely restarted."""


def service_target() -> str:
    return f"gui/{os.getuid()}/{BACKEND_LABEL}"


def require_current_process_managed() -> None:
    """Verify launchd owns this exact process before allowing a restart."""
    try:
        result = subprocess.run(
            [_LAUNCHCTL, "print", service_target()],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BackendRestartUnavailable("无法确认后端是否由 Bosun.app 管理") from exc

    match = re.search(r"^\s*pid = (\d+)\s*$", result.stdout, re.MULTILINE)
    if result.returncode != 0 or match is None or int(match.group(1)) != os.getpid():
        raise BackendRestartUnavailable("当前后端不是由 Bosun.app 管理，请先通过 Bosun.app 启动后端")


def restart_current_backend() -> None:
    """Restart only the backend LaunchAgent; the menubar app is untouched."""
    subprocess.run(
        [_LAUNCHCTL, "kickstart", "-k", service_target()],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
