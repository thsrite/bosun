"""访问口令与会话 token。

单用户工作台的最小鉴权：一个口令 + 服务端会话 token。
口令来源优先级：环境变量 BOSUN_PASSWORD > 设置页写入 DB 的 PBKDF2 哈希。
两者都没有 = 未启用登录（本机开发默认形态），此时全部接口放行。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time

from . import db

_PBKDF2_ROUNDS = 200_000
_SESSION_TTL = 30 * 24 * 3600  # 30 天
_PASSWORD_HASH_KEY = "auth_password_hash"
MIN_PASSWORD_LENGTH = 6

# WebSocket 鉴权子协议标记（token 作为紧随其后的第二个子协议传递）
WS_AUTH_SUBPROTOCOL = "bosun.auth"


def _env_password() -> str:
    # 兼容旧品牌变量 DECKHAND_PASSWORD：升级用户未改环境变量时不至于静默变空密码
    return (
        os.environ.get("BOSUN_PASSWORD")
        or os.environ.get("DECKHAND_PASSWORD")
        or ""
    ).strip()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${_PBKDF2_ROUNDS}${salt.hex()}${digest.hex()}"


def _verify_hash(password: str, encoded: str) -> bool:
    try:
        algo, rounds, salt_hex, digest_hex = encoded.split("$")
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(rounds))
    return hmac.compare_digest(digest.hex(), digest_hex)


def is_enabled() -> bool:
    """是否已启用登录。"""
    return bool(_env_password() or db.get_setting(_PASSWORD_HASH_KEY, ""))


def password_source() -> str:
    """口令来自哪里：env / db / none。设置页据此决定能否改密码。"""
    if _env_password():
        return "env"
    if db.get_setting(_PASSWORD_HASH_KEY, ""):
        return "db"
    return "none"


def verify_password(password: str) -> bool:
    env = _env_password()
    if env:
        return hmac.compare_digest(password, env)
    stored = db.get_setting(_PASSWORD_HASH_KEY, "")
    if not stored:
        return False
    return _verify_hash(password, stored)


def set_password(password: str) -> None:
    """写入新口令（哈希存 DB），并踢掉所有已发出的会话。"""
    db.set_setting(_PASSWORD_HASH_KEY, hash_password(password))
    revoke_all()


def clear_password() -> None:
    """关闭登录（仅对 DB 口令有效；env 口令得改环境变量）。"""
    db.set_setting(_PASSWORD_HASH_KEY, "")
    revoke_all()


# 登录失败退避：超过阈值后按来源锁定一段时间，堵暴力破解，也挡住
# 「每次请求都跑一遍 20 万轮 PBKDF2」造成的 CPU 打满。进程内内存态即可——
# 重启后清零无所谓，攻击者拿不到额外收益。
FAILURES_BEFORE_LOCKOUT = 5
LOCKOUT_SECONDS = 60.0
_failures: dict[str, tuple[int, float]] = {}


def lockout_remaining(source: str) -> float:
    """该来源还需等待多少秒才能再试；0 表示可以尝试。"""
    count, locked_until = _failures.get(source, (0, 0.0))
    return max(0.0, locked_until - time.time())


def record_login_failure(source: str) -> None:
    now = time.time()
    # 顺手丢弃早已解锁的陈旧来源，别让伪造来源把字典撑大
    for stale, (_, locked_until) in list(_failures.items()):
        if stale != source and locked_until and locked_until < now - LOCKOUT_SECONDS:
            del _failures[stale]
    count, _ = _failures.get(source, (0, 0.0))
    count += 1
    locked_until = now + LOCKOUT_SECONDS if count >= FAILURES_BEFORE_LOCKOUT else 0.0
    _failures[source] = (count, locked_until)


def clear_login_failures(source: str) -> None:
    _failures.pop(source, None)


def create_session() -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    db.execute(
        "INSERT INTO auth_session (token, created_at, expires_at) VALUES (?,?,?)",
        (token, now, now + _SESSION_TTL),
    )
    return token


def validate_token(token: str | None) -> bool:
    if not token:
        return False
    row = db.query_one("SELECT expires_at FROM auth_session WHERE token=?", (token,))
    if row is None:
        return False
    if row["expires_at"] < time.time():
        db.execute("DELETE FROM auth_session WHERE token=?", (token,))
        return False
    return True


def revoke(token: str | None) -> None:
    if token:
        db.execute("DELETE FROM auth_session WHERE token=?", (token,))


def revoke_all() -> None:
    db.execute("DELETE FROM auth_session", ())


def purge_expired() -> None:
    db.execute("DELETE FROM auth_session WHERE expires_at < ?", (time.time(),))


# ---- 任务回调 token ----------------------------------------------------
# agent 不是浏览器，登录不了，但收尾回报必须能送达；开了访问口令后
# 全局中间件会把这类无凭证回调一律 401 掉(任务状态永远进不了 waiting_input，
# 待处理列表因此空掉)。故每次派发任务时随机发一枚只对该任务、只对 /report
# 端点有效的 token，随环境变量交给 agent。


def issue_task_token(task_id: int) -> str:
    """给本轮运行发一枚任务回调 token（覆盖上一轮的，旧 token 随即失效）。"""
    token = secrets.token_urlsafe(24)
    db.execute("UPDATE task SET report_token=? WHERE id=?", (token, task_id))
    return token


def validate_task_token(task_id: int, token: str | None) -> bool:
    """token 是否是该任务当前这轮的回调凭证。"""
    if not token:
        return False
    row = db.query_one("SELECT report_token FROM task WHERE id=?", (task_id,))
    if row is None or not row["report_token"]:
        return False
    return hmac.compare_digest(str(row["report_token"]), token)


def token_from_request(headers) -> str | None:
    """从 Authorization: Bearer 头取 token。"""
    raw = headers.get("authorization") or ""
    if raw.lower().startswith("bearer "):
        return raw[7:].strip()
    return None


def token_from_subprotocols(header_value: str | None) -> str | None:
    """从 Sec-WebSocket-Protocol 头取 token。

    浏览器的 WebSocket 无法设置自定义请求头，唯二的选择是 query 参数或子协议。
    query 会被 uvicorn 的 access log 连同 URL 整条记下来，等于把长期有效的
    token 写进日志文件，所以这里走子协议——它只存在于握手头里，不进 access log。
    约定的形式：Sec-WebSocket-Protocol: bosun.auth, <token>
    """
    parts = [p.strip() for p in (header_value or "").split(",") if p.strip()]
    if len(parts) >= 2 and parts[0] == WS_AUTH_SUBPROTOCOL:
        return parts[1]
    return None
