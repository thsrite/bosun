"""历史任务日志的 gzip 归档与透明读取。

已终结任务的日志压成同名 .gz 并删除原文件：设置页「压缩历史日志」立即全量压缩，
调度器另外定期把闲置超过 AUTO_ARCHIVE_IDLE_SECONDS 的日志自动归档。
读取侧统一走本模块：原文件缺失时自动回退读 .gz，历史日志压缩后仍可查看。
"""
from __future__ import annotations

import gzip
import shutil
import time
from pathlib import Path

# 日志最后一次写入超过这么久、且任务已终结，才自动归档；手动压缩不受此限
AUTO_ARCHIVE_IDLE_SECONDS = 3 * 24 * 3600


def gz_path(path: str | Path) -> Path:
    return Path(f"{path}.gz")


def has_log(path: str | Path) -> bool:
    """原文件或对应压缩包任一存在。"""
    p = Path(path)
    return p.exists() or gz_path(p).exists()


def has_content(path: str | Path) -> bool:
    """原文件非空，或存在压缩包（空文件不会被压缩，.gz 必有内容）。"""
    p = Path(path)
    try:
        if p.exists():
            return p.stat().st_size > 0
    except OSError:
        return False
    return gz_path(p).exists()


def read_text(path: str | Path) -> str | None:
    """读日志全文；原文件缺失时回退读压缩包，都没有返回 None。"""
    p = Path(path)
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        pass
    except OSError:
        return None
    try:
        with gzip.open(gz_path(p), "rt", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def read_tail(path: str | Path, max_bytes: int) -> bytes | None:
    """读日志末尾 max_bytes 字节；压缩包需整体解压后截尾。"""
    p = Path(path)
    try:
        with p.open("rb") as f:
            if p.stat().st_size > max_bytes:
                f.seek(-max_bytes, 2)
            return f.read()
    except FileNotFoundError:
        pass
    except OSError:
        return None
    try:
        with gzip.open(gz_path(p), "rb") as f:
            data = f.read()
    except OSError:
        return None
    return data[-max_bytes:]


def compress(path: Path) -> int:
    """把单个日志压成 .gz 并删除原文件，返回节省的字节数；压缩失败时原文件保留。"""
    gz = gz_path(path)
    orig_size = path.stat().st_size
    with path.open("rb") as src, gzip.open(gz, "wb") as dst:
        shutil.copyfileobj(src, dst)
    saved = orig_size - gz.stat().st_size
    path.unlink()
    return saved


def archive_ended_logs(min_idle_seconds: float = 0) -> tuple[int, int]:
    """把已终结任务（done/failed/cancelled）的日志 gzip 归档，返回 (归档数, 节省字节)。

    进行中或可恢复的任务（queued/running/waiting_input/paused/interrupted）与运行中的
    autopilot 日志不动；min_idle_seconds 过滤掉最近还写过的日志。
    """
    from . import config, db
    from .pty_session import script_log_path_for

    protected: set[str] = set()
    for row in db.query(
        "SELECT log_path FROM task "
        "WHERE log_path IS NOT NULL AND status NOT IN ('done','failed','cancelled')"
    ):
        protected.add(row["log_path"])
        protected.add(script_log_path_for(row["log_path"]))
    for row in db.query(
        "SELECT log_path FROM autopilot_run "
        "WHERE log_path IS NOT NULL AND status='running'"
    ):
        protected.add(row["log_path"])
    cutoff = time.time() - min_idle_seconds
    count = 0
    saved = 0
    for path in config.LOG_DIR.iterdir():
        if not path.is_file() or path.suffix == ".gz" or str(path) in protected:
            continue
        try:
            st = path.stat()
            if st.st_size == 0 or st.st_mtime > cutoff:
                continue
            saved += compress(path)
        except OSError:
            continue
        count += 1
    return count, saved


def remove(path: str | Path) -> None:
    """删除日志的原文件与压缩包（任务删除时清理用）。"""
    for target in (Path(path), gz_path(path)):
        try:
            target.unlink()
        except OSError:
            pass
