"""Bosun 版本号的权威来源。

发版时同步改这里和 frontend/package.json，并打上同名 tag（vX.Y.Z）——
自更新用 tag 与本值比对，两者不一致会导致「已是最新版」误判。
"""
from __future__ import annotations

VERSION = "0.4.1"
