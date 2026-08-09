"""cc / codex 订阅限额查询(参考 cc-switch / usage4claude 逆向的非官方端点)。

⚠️ 非官方接口，可能随官方变更失效；只读用量，不消耗 LLM 配额。
- Claude: GET api.anthropic.com/api/oauth/usage  (token 取自 macOS keychain)
  限流极凶 → 最小轮询间隔 180s。
- Codex:  GET chatgpt.com/backend-api/wham/usage  (token 取自 ~/.codex/auth.json)
"""
from __future__ import annotations

import json
import re
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import certifi

from . import config
from .env import child_env

_SSL_CTX = ssl.create_default_context(cafile=certifi.where())

CLAUDE_MIN_INTERVAL = 185
CODEX_MIN_INTERVAL = 60

_cache: dict[str, dict] = {}   # provider -> {"at": ts, "data": {...}}
_claude_ver: str | None = None
_ENGINE_PROVIDER = {"cc": "claude", "codex": "codex"}
_SEVEN_DAY_SECONDS = 7 * 24 * 60 * 60


def _claude_version() -> str:
    global _claude_ver
    if _claude_ver is None:
        try:
            out = subprocess.run(
                [config.CLAUDE_BIN, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                env=child_env({"NO_COLOR": "1", "FORCE_COLOR": "0"}),
            ).stdout
            match = re.search(r"(?<!\d)(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)", out)
            _claude_ver = match.group(1) if match else (out.split()[0].strip() or "2.1.0")
        except Exception:  # noqa: BLE001
            _claude_ver = "2.1.0"
    return _claude_ver


def _get(url: str, headers: dict, timeout: int = 15) -> tuple[int, dict | None]:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:  # noqa: BLE001
        return -1, None


# ---- 凭据 ----
def _claude_token() -> str | None:
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        obj = json.loads(out)
        return (obj.get("claudeAiOauth") or {}).get("accessToken") or obj.get("accessToken")
    except Exception:  # noqa: BLE001
        return None


def _claude_login_state() -> bool | None:
    """Ask Claude CLI whether its refreshable login session is still valid."""
    try:
        result = subprocess.run(
            [config.CLAUDE_BIN, "auth", "status"],
            capture_output=True,
            text=True,
            timeout=10,
            env=child_env({"NO_COLOR": "1", "FORCE_COLOR": "0"}),
        )
        if result.returncode != 0:
            return None
        logged_in = json.loads(result.stdout).get("loggedIn")
        return logged_in if isinstance(logged_in, bool) else None
    except Exception:  # noqa: BLE001
        return None


def _codex_creds() -> tuple[str | None, str | None]:
    try:
        obj = json.loads((Path.home() / ".codex" / "auth.json").read_text())
        t = obj.get("tokens") or {}
        return t.get("access_token"), t.get("account_id")
    except Exception:  # noqa: BLE001
        return None, None


# ---- 解析 ----
def _pct(v) -> float | None:
    if isinstance(v, (int, float)):
        return round(float(v), 1)
    return None


def _fetch_claude() -> dict:
    token = _claude_token()
    if not token:
        return {"available": False, "error": "未找到 Claude 凭据(keychain)，请在终端登录 claude"}
    status, data = _get(
        "https://api.anthropic.com/api/oauth/usage",
        {
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": f"claude-code/{_claude_version()}",
        },
    )
    if status == 401:
        login_state = _claude_login_state()
        if login_state is True:
            return {
                "available": False,
                "error": "Claude 用量凭据待刷新；Claude CLI 仍已登录，请在 Claude 中发起一次请求后刷新",
            }
        if login_state is False:
            return {"available": False, "error": "Claude 登录已失效，请重新登录"}
        return {"available": False, "error": "Claude 用量接口鉴权失败(HTTP 401)"}
    if status == 429:
        return {"available": False, "error": "Claude 用量接口被限流(稍后重试)"}
    if status != 200 or not data:
        return {"available": False, "error": f"Claude 用量接口异常(HTTP {status})"}
    fh = data.get("five_hour") or {}
    sd = data.get("seven_day") or {}
    return {
        "available": True,
        "error": None,
        "five_hour_pct": _pct(fh.get("utilization")),
        "weekly_pct": _pct(sd.get("utilization")),
        "five_hour_resets_at": fh.get("resets_at"),
        "weekly_resets_at": sd.get("resets_at"),
    }


def _find_window(data: dict, keys: list[str]) -> dict | None:
    """在 codex 返回里按候选键找一个窗口对象。"""
    for k in keys:
        v = data.get(k)
        if isinstance(v, dict):
            return v
    rl = data.get("rate_limit") or data.get("rate_limits") or data.get("rateLimits")
    if isinstance(rl, dict):
        for k in keys:
            if isinstance(rl.get(k), dict):
                return rl[k]
    return None


def _win_pct(w: dict | None) -> float | None:
    if not isinstance(w, dict):
        return None
    for k in ("used_percent", "used_percentage", "utilization", "percent_used"):
        if k in w:
            return _pct(w[k])
    return None


def _win_reset(w: dict | None):
    if not isinstance(w, dict):
        return None
    for k in ("resets_at", "reset_at", "resets_in_seconds", "reset_after_seconds"):
        if k in w:
            return w[k]
    return None


def _codex_weekly_window(primary: dict | None, secondary: dict | None) -> dict | None:
    """codex 只剩 7 天窗（5 小时限额已取消）；旧响应无时长时按位置取 secondary。"""
    windows = [w for w in (primary, secondary) if isinstance(w, dict)]
    if not any("limit_window_seconds" in w for w in windows):
        return secondary
    for window in windows:
        if window.get("limit_window_seconds") == _SEVEN_DAY_SECONDS:
            return window
    return None


def _fetch_codex() -> dict:
    token, account = _codex_creds()
    if not token:
        return {"available": False, "error": "未找到 Codex 凭据(~/.codex/auth.json)，请登录 codex"}
    headers = {"Authorization": f"Bearer {token}"}
    if account:
        headers["ChatGPT-Account-Id"] = account
    status, data = _get("https://chatgpt.com/backend-api/wham/usage", headers)
    if status == 401:
        return {"available": False, "error": "Codex token 过期，请重新登录"}
    if status == 403:
        return {"available": False, "error": "Codex 账号无权访问用量接口"}
    if status != 200 or not data:
        return {"available": False, "error": f"Codex 用量接口异常(HTTP {status})"}
    primary = _find_window(data, ["primary_window", "primary", "five_hour_limit", "five_hour"])
    secondary = _find_window(data, ["secondary_window", "weekly_limit", "weekly", "seven_day"])
    weekly = _codex_weekly_window(primary, secondary)
    return {
        "available": True,
        "error": None,
        "plan": data.get("plan_type"),
        "weekly_pct": _win_pct(weekly),
        "weekly_resets_at": _win_reset(weekly),
    }


def _cached(provider: str, min_interval: int, fetch) -> dict:
    now = time.time()
    c = _cache.get(provider)
    if c and now - c["at"] < min_interval:
        return {**c["data"], "cached_age": int(now - c["at"])}
    data = fetch()
    _cache[provider] = {"at": now, "data": data}
    return {**data, "cached_age": 0}


def block_pct() -> int:
    from . import db

    try:
        return int(db.get_setting("quota_block_pct", 90))
    except (TypeError, ValueError):
        return 90


def _provider_usage(provider: str) -> dict:
    if provider == "claude":
        return _cached("claude", CLAUDE_MIN_INTERVAL, _fetch_claude)
    if provider == "codex":
        return _cached("codex", CODEX_MIN_INTERVAL, _fetch_codex)
    return {"available": False, "error": f"未知服务商: {provider}"}


def get_usage(engine: str | None = None) -> dict:
    if engine:
        provider = _ENGINE_PROVIDER.get(engine)
        if provider:
            return {provider: _provider_usage(provider), "block_pct": block_pct()}
    return {
        "claude": _provider_usage("claude"),
        "codex": _provider_usage("codex"),
        "block_pct": block_pct(),
    }


def check_engine(engine: str) -> tuple[bool, float | None, str]:
    """返回 (是否可执行, 最高窗口用量%, 原因)。用量拿不到时放行(不拦)。"""
    provider = _ENGINE_PROVIDER.get(engine)
    usage = _provider_usage(provider) if provider else None
    if not usage or not usage.get("available"):
        return True, None, "用量未知，放行"
    limit = block_pct()
    windows = [
        ("5 小时", usage.get("five_hour_pct")),
        ("周", usage.get("weekly_pct")),
    ]
    known = [(label, float(pct)) for label, pct in windows if isinstance(pct, (int, float))]
    exceeded = [(label, pct) for label, pct in known if pct >= limit]
    highest = max((pct for _, pct in known), default=None)
    if exceeded:
        detail = "、".join(f"{label}用量已 {pct:g}%" for label, pct in exceeded)
        return False, highest, f"{engine} {detail}（≥{limit}% 阈值）"
    return True, highest, "ok"
