"""全局配置与数据目录。"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
from pathlib import Path

# PyInstaller 冻结包：随包资源（frontend/dist、bosun_skills）解压在 _MEIPASS 下；
# 源码运行时资源根即仓库根。凡随仓库分发的只读资源一律经 RESOURCE_ROOT 解析。
IS_FROZEN = bool(getattr(sys, "frozen", False))
RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", "")) if IS_FROZEN else Path(__file__).resolve().parents[2]

# 数据根目录：~/.bosun（旧版 ~/.deckhand 会在首次启动时自动迁移）
_ENV_DATA = os.environ.get("BOSUN_DATA")
DATA_DIR = Path(_ENV_DATA) if _ENV_DATA else Path.home() / ".bosun"
_LEGACY_DIR = Path.home() / ".deckhand"


def _migrate_legacy_data_dir() -> None:
    """一次性把旧的 ~/.deckhand 迁移到 ~/.bosun。

    仅在使用默认路径、目标目录尚不存在、且旧目录存在时执行；同盘为原子 rename。
    数据库文件名 deckhand.db 一并改为 bosun.db（含 SQLite 的 -wal/-shm 附属文件）。
    """
    if _ENV_DATA or DATA_DIR.exists() or not _LEGACY_DIR.is_dir():
        return
    try:
        _LEGACY_DIR.rename(DATA_DIR)
    except OSError:
        # 跨盘等 rename 失败时退回复制
        shutil.copytree(_LEGACY_DIR, DATA_DIR)
    for suffix in ("", "-wal", "-shm"):
        src = DATA_DIR / f"deckhand.db{suffix}"
        dst = DATA_DIR / f"bosun.db{suffix}"
        if src.exists() and not dst.exists():
            src.rename(dst)
    # db 里指向旧数据目录的绝对路径一并迁移，否则历史任务日志会断链
    _rewrite_db_legacy_paths(DATA_DIR / "bosun.db")


def _rewrite_db_legacy_paths(db_path: Path) -> None:
    """把 db 中 log_path 里的旧数据目录前缀（~/.deckhand/）改写为新目录（~/.bosun/）。

    仅匹配带前导点的家目录数据路径，不会误伤仓库物理路径（PycharmProjects/deckhand）。
    """
    if not db_path.exists():
        return
    old = f"{_LEGACY_DIR}/"
    new = f"{DATA_DIR}/"
    con = sqlite3.connect(db_path)
    try:
        cur = con.cursor()
        for (table,) in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall():
            cols = [r[1] for r in cur.execute(f"PRAGMA table_info({table})").fetchall()]
            if "log_path" in cols:
                cur.execute(
                    f"UPDATE {table} SET log_path=REPLACE(log_path, ?, ?) "
                    f"WHERE log_path LIKE ?",
                    (old, new, f"%{old}%"),
                )
        con.commit()
    finally:
        con.close()


_migrate_legacy_data_dir()

LOG_DIR = DATA_DIR / "logs"
DB_PATH = DATA_DIR / "bosun.db"

DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

# 默认并发上限（可在设置里改，存 DB）
DEFAULT_MAX_CONCURRENT = 3

# 低置信 waiting_input 判定：静默超过该秒数后，还必须看到输入提示形态
IDLE_SECONDS = 8.0

def _augment_path_env() -> None:
    """把常见的用户级 bin 目录补进本进程 PATH。

    launchd / 服务方式启动时不读 shell rc，bun、uv 等安装器的目录（如 ~/.bun/bin）
    会缺失，导致装好的引擎被误判为未安装、PTY 子进程也找不到命令。
    只追加确实存在且尚未在 PATH 中的目录，不改动已有顺序。
    """
    extras = [
        Path.home() / ".bun" / "bin",
        Path.home() / ".local" / "bin",
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
    ]
    current = os.environ.get("PATH", "")
    parts = current.split(os.pathsep) if current else []
    added = [str(d) for d in extras if d.is_dir() and str(d) not in parts]
    if added:
        os.environ["PATH"] = os.pathsep.join(parts + added)


_augment_path_env()


def _resolve_bin(env_name: str, default: str, candidates: list[Path]) -> str:
    configured = os.environ.get(env_name)
    if configured:
        return configured
    resolved = shutil.which(default)
    if resolved:
        return resolved
    if sys.platform == "win32":
        # npm 全局包在 Windows 落在 %APPDATA%\npm 下的 .cmd shim
        appdata = os.environ.get("APPDATA")
        if appdata:
            for ext in (".cmd", ".exe"):
                shim = Path(appdata) / "npm" / f"{default}{ext}"
                if shim.is_file():
                    return str(shim)
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return default


# 引擎可执行文件名（可用环境变量覆盖）
CLAUDE_BIN = _resolve_bin(
    "BOSUN_CLAUDE_BIN",
    "claude",
    [
        Path.home() / ".local" / "bin" / "claude",
        Path.home() / ".bun" / "bin" / "claude",
        Path("/opt/homebrew/bin/claude"),
        Path("/usr/local/bin/claude"),
    ],
)
CODEX_BIN = _resolve_bin(
    "BOSUN_CODEX_BIN",
    "codex",
    [
        Path.home() / ".local" / "bin" / "codex",
        Path.home() / ".bun" / "bin" / "codex",
        Path("/opt/homebrew/bin/codex"),
        Path("/usr/local/bin/codex"),
    ],
)
OMP_BIN = _resolve_bin(
    "BOSUN_OMP_BIN",
    "omp",
    [
        Path.home() / ".local" / "bin" / "omp",
        Path.home() / ".bun" / "bin" / "omp",
        Path("/opt/homebrew/bin/omp"),
        Path("/usr/local/bin/omp"),
    ],
)
KIMI_BIN = _resolve_bin(
    "BOSUN_KIMI_BIN",
    "kimi",
    [
        Path.home() / ".local" / "bin" / "kimi",
        Path.home() / ".bun" / "bin" / "kimi",
        Path.home() / ".kimi-code" / "bin" / "kimi",
        Path("/opt/homebrew/bin/kimi"),
        Path("/usr/local/bin/kimi"),
    ],
)
