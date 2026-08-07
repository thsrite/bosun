"""历史任务日志的 gzip 归档与透明读取。

压缩由设置页「压缩历史日志」触发：已终结任务的日志压成同名 .gz 并删除原文件。
读取侧统一走本模块：原文件缺失时自动回退读 .gz，历史日志压缩后仍可查看。
"""
from __future__ import annotations

import gzip
import shutil
from pathlib import Path


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


def remove(path: str | Path) -> None:
    """删除日志的原文件与压缩包（任务删除时清理用）。"""
    for target in (Path(path), gz_path(path)):
        try:
            target.unlink()
        except OSError:
            pass
