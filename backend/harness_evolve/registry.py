"""Harness Registry：per-engine 版本化、快照式编辑、晋升/回滚、灰度分流。

版本快照不可变：propose 基于 active 快照拷贝后应用编辑生成新 shadow 版本，
父版本内容永不改写，回滚 = 状态机切换，天然 O(1)。
"""
from __future__ import annotations

import hashlib
from typing import Optional

from .models import OPS, SURFACES, EditError, HarnessEdit, HarnessVersion, RenderedHarness
from .store import Store


def _apply_edit(snapshot: dict, edit: HarnessEdit) -> HarnessEdit:
    """就地应用一条编辑，返回回填 old_value 后的编辑记录。"""
    if edit.surface not in SURFACES:
        raise EditError(f"非法 surface: {edit.surface}")
    if edit.op not in OPS:
        raise EditError(f"非法 op: {edit.op}")
    if not edit.entry_key:
        raise EditError("entry_key 不能为空")
    entries = snapshot.setdefault(edit.surface, {})
    exists = edit.entry_key in entries
    if edit.op == "add":
        if exists:
            raise EditError(f"add 的 key 已存在: {edit.surface}/{edit.entry_key}")
        if edit.new_value is None:
            raise EditError("add 缺少 new_value")
        entries[edit.entry_key] = edit.new_value
        return HarnessEdit(edit.surface, "add", edit.entry_key, edit.new_value, None, edit.provenance)
    if not exists:
        raise EditError(f"{edit.op} 的 key 不存在: {edit.surface}/{edit.entry_key}")
    old = entries[edit.entry_key]
    if edit.op == "update":
        if edit.new_value is None:
            raise EditError("update 缺少 new_value")
        entries[edit.entry_key] = edit.new_value
        return HarnessEdit(edit.surface, "update", edit.entry_key, edit.new_value, old, edit.provenance)
    del entries[edit.entry_key]
    return HarnessEdit(edit.surface, "remove", edit.entry_key, None, old, edit.provenance)


class Registry:
    def __init__(self, store: Store):
        self.store = store

    def init_engine(self, engine: str, entries: dict) -> HarnessVersion:
        if self.store.find_version(engine, "active"):
            raise EditError(f"引擎 {engine} 已初始化")
        for surface in entries:
            if surface not in SURFACES:
                raise EditError(f"非法 surface: {surface}")
        version = self.store.insert_version(engine, 1, "active", None)
        self.store.write_entries(version.id, entries)
        return version

    def get(self, version_id: int) -> HarnessVersion:
        version = self.store.get_version(version_id)
        if not version:
            raise EditError(f"版本不存在: {version_id}")
        return version

    def active(self, engine: str) -> Optional[HarnessVersion]:
        return self.store.find_version(engine, "active")

    def shadow(self, engine: str) -> Optional[HarnessVersion]:
        return self.store.find_version(engine, "shadow")

    def snapshot(self, version_id: int) -> dict:
        self.get(version_id)
        return self.store.read_entries(version_id)

    def edits_of(self, version_id: int) -> list:
        return self.store.read_edits(version_id)

    def propose(self, engine: str, edits: list) -> HarnessVersion:
        base = self.active(engine)
        if not base:
            raise EditError(f"引擎 {engine} 未初始化")
        snapshot = self.store.read_entries(base.id)
        applied = [_apply_edit(snapshot, e) for e in edits]
        version = self.store.insert_version(engine, self.store.max_version(engine) + 1,
                                            "shadow", base.id)
        self.store.write_entries(version.id, snapshot)
        self.store.write_edits(version.id, applied)
        return version

    def promote(self, version_id: int) -> HarnessVersion:
        version = self.get(version_id)
        if version.status != "shadow":
            raise EditError(f"只有 shadow 版本可晋升（当前 {version.status}）")
        current = self.active(version.engine)
        if current:
            self.store.set_status(current.id, "superseded")
        self.store.set_status(version.id, "active")
        return self.get(version.id)

    def rollback(self, engine: str) -> HarnessVersion:
        current = self.active(engine)
        if not current:
            raise EditError(f"引擎 {engine} 无 active 版本")
        if current.parent_id is None:
            raise EditError("根版本无可回滚的父版本")
        self.store.set_status(current.id, "rolled_back")
        self.store.set_status(current.parent_id, "active")
        return self.get(current.parent_id)

    def sync_protected_entry(self, engine: str, surface: str, key: str, value: str) -> int:
        """宿主升级改了受保护条目的发布文本时，把该引擎所有版本刷成新文本。

        受保护 key（Gate-0 protected_keys）演进提案禁改，因此跨版本统一覆盖
        不会丢任何演进成果；返回受影响行数。
        """
        if surface not in SURFACES:
            raise EditError(f"非法 surface: {surface}")
        return self.store.update_entry_all_versions(engine, surface, key, value)

    def choose(self, engine: str, dispatch_key: str, shadow_percent: int) -> HarnessVersion:
        """灰度分流：宿主每次派发前调用。确定性哈希，同 key 恒同结果。"""
        active = self.active(engine)
        if not active:
            raise EditError(f"引擎 {engine} 未初始化")
        shadow = self.shadow(engine)
        if not shadow or shadow_percent <= 0:
            return active
        bucket = int.from_bytes(
            hashlib.sha256(f"{engine}:{dispatch_key}".encode()).digest()[:4], "big") % 100
        return shadow if bucket < shadow_percent else active

    def render(self, version_id: int) -> RenderedHarness:
        snapshot = self.snapshot(version_id)
        directive = "\n\n".join(v for _, v in sorted(snapshot.get("directive", {}).items()))
        return RenderedHarness(
            version_id=version_id,
            directive_text=directive,
            policy=dict(snapshot.get("policy", {})),
            skills=dict(snapshot.get("skill", {})),
            memory=sorted(snapshot.get("memory", {}).items()),
        )
