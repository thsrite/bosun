"""独立 SQLite 存储：自带 schema（he_* 前缀），不复用宿主的任何表。

连接由宿主注入（可与宿主同库共存，也可独立文件），本模块只建/读写 he_* 表。
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from .models import HarnessEdit, HarnessVersion

_SCHEMA = """
CREATE TABLE IF NOT EXISTS he_version (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    engine TEXT NOT NULL,
    version INTEGER NOT NULL,
    status TEXT NOT NULL,
    parent_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS he_entry (
    version_id INTEGER NOT NULL,
    surface TEXT NOT NULL,
    entry_key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (version_id, surface, entry_key)
);
CREATE TABLE IF NOT EXISTS he_edit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL,
    surface TEXT NOT NULL,
    op TEXT NOT NULL,
    entry_key TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    provenance TEXT
);
CREATE INDEX IF NOT EXISTS idx_he_version_engine ON he_version(engine, status);
"""


class Store:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def _row_version(self, row) -> HarnessVersion:
        return HarnessVersion(id=row[0], engine=row[1], version=row[2], status=row[3],
                              parent_id=row[4], created_at=row[5])

    def insert_version(self, engine: str, version: int, status: str,
                       parent_id: Optional[int]) -> HarnessVersion:
        cur = self.conn.execute(
            "INSERT INTO he_version(engine, version, status, parent_id) VALUES (?,?,?,?)",
            (engine, version, status, parent_id))
        self.conn.commit()
        return self.get_version(cur.lastrowid)

    def get_version(self, version_id: int) -> Optional[HarnessVersion]:
        row = self.conn.execute(
            "SELECT id, engine, version, status, parent_id, created_at FROM he_version WHERE id=?",
            (version_id,)).fetchone()
        return self._row_version(row) if row else None

    def find_version(self, engine: str, status: str) -> Optional[HarnessVersion]:
        row = self.conn.execute(
            "SELECT id, engine, version, status, parent_id, created_at FROM he_version"
            " WHERE engine=? AND status=? ORDER BY id DESC LIMIT 1",
            (engine, status)).fetchone()
        return self._row_version(row) if row else None

    def set_status(self, version_id: int, status: str) -> None:
        self.conn.execute("UPDATE he_version SET status=? WHERE id=?", (status, version_id))
        self.conn.commit()

    def max_version(self, engine: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(version), 0) FROM he_version WHERE engine=?", (engine,)).fetchone()
        return int(row[0])

    def write_entries(self, version_id: int, snapshot: dict) -> None:
        rows = [(version_id, surface, key, value)
                for surface, entries in snapshot.items()
                for key, value in entries.items()]
        self.conn.executemany(
            "INSERT INTO he_entry(version_id, surface, entry_key, value) VALUES (?,?,?,?)", rows)
        self.conn.commit()

    def update_entry_all_versions(self, engine: str, surface: str,
                                  entry_key: str, value: str) -> int:
        """把某引擎所有版本里的同名条目统一改为 value，返回受影响行数。

        供宿主同步 Gate-0 受保护条目用：受保护 key 不参与演进，各版本文本
        理应恒等于宿主当前发布的内容。
        """
        cur = self.conn.execute(
            "UPDATE he_entry SET value=? WHERE surface=? AND entry_key=? AND value<>?"
            " AND version_id IN (SELECT id FROM he_version WHERE engine=?)",
            (value, surface, entry_key, value, engine))
        self.conn.commit()
        return cur.rowcount

    def read_entries(self, version_id: int) -> dict:
        snapshot: dict = {}
        for surface, key, value in self.conn.execute(
                "SELECT surface, entry_key, value FROM he_entry WHERE version_id=?", (version_id,)):
            snapshot.setdefault(surface, {})[key] = value
        return snapshot

    def write_edits(self, version_id: int, edits: list) -> None:
        self.conn.executemany(
            "INSERT INTO he_edit(version_id, surface, op, entry_key, old_value, new_value, provenance)"
            " VALUES (?,?,?,?,?,?,?)",
            [(version_id, e.surface, e.op, e.entry_key, e.old_value, e.new_value, e.provenance)
             for e in edits])
        self.conn.commit()

    def read_edits(self, version_id: int) -> list:
        return [HarnessEdit(surface=r[0], op=r[1], entry_key=r[2], old_value=r[3],
                            new_value=r[4], provenance=r[5])
                for r in self.conn.execute(
                    "SELECT surface, op, entry_key, old_value, new_value, provenance"
                    " FROM he_edit WHERE version_id=? ORDER BY id", (version_id,))]
