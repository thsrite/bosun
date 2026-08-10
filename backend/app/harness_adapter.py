"""harness_evolve 核心包的 Bosun 适配层。

依赖方向：engines/scheduler → 本模块 → backend.harness_evolve（core 不知道 Bosun）。
he_* 表与 Bosun 主库同文件共存（core 自建 schema，不触碰 Bosun 的表）。
演进层任何故障都不得阻断派发：directive_for 失败一律回退静态 REPORT_DIRECTIVE。
"""
from __future__ import annotations

import logging
from typing import Optional

from backend.harness_evolve import Registry, Store

from . import db
from .directives import REPORT_DIRECTIVE

log = logging.getLogger(__name__)

# 收尾约定条目：种子内容取自 engines.REPORT_DIRECTIVE；该 key 属 Gate-0 受保护
# 条目（演进提案禁改），编号前缀留出 1xx-2xx 给未来的高优先级指令。
REPORT_KEY = "100-report"

_registry: Optional[Registry] = None


def get_registry() -> Registry:
    global _registry
    if _registry is None:
        _registry = Registry(Store(db.get_conn()))
    return _registry


def _ensure_seeded(registry: Registry, engine: str) -> None:
    if registry.active(engine) is None:
        registry.init_engine(engine, {"directive": {REPORT_KEY: REPORT_DIRECTIVE}})


def directive_for(engine: str, dispatch_key: str = "") -> str:
    """取该引擎当前生效 harness 的 directive 文本（含灰度分流）。

    dispatch_key 传任务 id 之类的稳定键；MVP 期无 shadow 流量（灰度 0%），
    传空即恒走 active。
    """
    try:
        registry = get_registry()
        _ensure_seeded(registry, engine)
        version = registry.choose(engine, dispatch_key or "-", _shadow_percent())
        return registry.render(version.id).directive_text
    except Exception:
        log.exception("harness registry 故障，回退静态收尾约定 (engine=%s)", engine)
        return REPORT_DIRECTIVE


def _shadow_percent() -> int:
    """灰度比例；Gate-1 实装（v1）前恒为 0，届时改由 settings 读取。"""
    return 0
