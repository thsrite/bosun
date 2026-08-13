"""项目附件落盘：普通任务与编排运行共用。"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

MAX_UPLOAD_BYTES = 50 * 1024 * 1024


def _ensure_gitignored(project_root: Path, entry: str) -> None:
    if not (project_root / ".git").exists():
        return
    gitignore = project_root / ".gitignore"
    try:
        lines = gitignore.read_text(encoding="utf-8").splitlines() if gitignore.exists() else []
        if any(line.strip() == entry for line in lines):
            return
        prefix = "" if not lines or lines[-1] == "" else "\n"
        with gitignore.open("a", encoding="utf-8") as handle:
            handle.write(f"{prefix}{entry}\n")
    except OSError:
        pass


def save_project_upload(project_path: str, filename: str, data: bytes) -> str:
    if not data:
        raise ValueError("空文件")
    if len(data) > MAX_UPLOAD_BYTES:
        raise OverflowError("文件过大（上限 50MB）")
    project_root = Path(project_path)
    destination_dir = project_root / ".bosun-uploads"
    destination_dir.mkdir(parents=True, exist_ok=True)
    _ensure_gitignored(project_root, ".bosun-uploads/")
    base = os.path.basename(filename or "file")
    extension = os.path.splitext(base)[1].lower()
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.splitext(base)[0]) or "file"
    destination = destination_dir / f"{int(time.time() * 1000)}_{stem}{extension}"
    destination.write_bytes(data)
    return str(destination)
