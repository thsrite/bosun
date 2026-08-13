"""限流识别 + 退避计算（零依赖底层，pty_session 与 scheduler 共同引用）。

判据取自本机真实 PTY 日志采样，核心结论是**宁可漏判也不能误判**：
误判会把一个正常运行的任务挂起等一小时，比漏判（退回原有的超时失败）糟得多。

真实语料里含 "usage limit" / "rate limit" / "429" 的行绝大多数**不是**限流：
  · `You have 1 usage limit reset available. Run /usage`  ← 有重置额度可用，恰恰没被限
  · `Approaching rate limits`                             ← 接近阈值，还能跑
  · `Hide future rate limit reminders about switching models.` ← 设置项文案
  · `HTTPException(status_code=429, ...)` / `test_..._gets_429` ← agent 正在读写的源码
因此：**裸 429、裸 "rate limit"、裸 "usage limit" 一律不作判据**，只认「已经撞上」
这一语义的完整短语（hit/reached/exceeded），并对上面几类显式反匹配。

匹配对象是 pty_session 剥掉 ANSI 之后的文本。剥离会把光标定位序列(如 `\x1b[58G`)
去掉，导致原本分列渲染的文字无空格粘连（真实样本：
`try again at Aug 8th, 2026 2:12 PM.Approaching rate limits`），所以正则不能依赖词间空格。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime

# 「已经撞上限流」的完整语义，缺一不可：必须同时出现「撞上/耗尽」+「额度」。
_HIT_RES = [
    # codex: You've hit your usage limit. / You have hit your usage limit.
    re.compile(r"\b(?:you'?ve|you\s+have)\s+hit\s+your\s+(?:usage\s+)?limit", re.I),
    # claude code: Claude usage limit reached
    re.compile(r"\busage\s+limit\s+reached\b", re.I),
    re.compile(r"\b(?:rate|usage)\s+limit\s+exceeded\b", re.I),
    re.compile(r"\bquota\s+exceeded\b", re.I),
    # 明确的 HTTP 语义（带 Retry-After 才算，裸 429 是源码噪声）
    re.compile(r"\bretry-after\s*[:=]\s*\d+", re.I),
]

# 显式反匹配：命中任意一条则整段判定为「非限流」。全部取自真实日志。
_NOT_HIT_RES = [
    # 「你有 N 次重置额度可用」——含 usage limit 但语义相反
    re.compile(r"\busage\s+limit\s+reset\s+available", re.I),
    # 「接近」阈值，不是已经撞上
    re.compile(r"\bapproaching\s+rate\s+limits?\b", re.I),
    # 设置项/提醒文案
    re.compile(r"\bhide\s+future\s+rate\s+limit\s+reminders?", re.I),
    # 额度说明书（"you can use up to half of your weekly usage limit"）
    re.compile(r"\bcan\s+use\s+up\s+to\b[^\n]{0,80}\blimit\b", re.I),
]

# 恢复时间：codex 真实文案 `try again at Aug 8th, 2026 2:12 PM`
_RESET_AT_RE = re.compile(
    r"try\s+again\s+at\s+"
    r"([A-Z][a-z]{2})\w*\s*(\d{1,2})(?:st|nd|rd|th)?\s*,?\s*(\d{4})\s*"
    r"(\d{1,2}):(\d{2})\s*([AP]M)",
    re.I,
)

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_FALSE = {"0", "false", "no", "off"}

BACKOFF_BASE_SECONDS = 300.0   # 首次退避 5 分钟
BACKOFF_CAP_SECONDS = 3600.0   # 封顶 1 小时


@dataclass
class RateLimitHit:
    """一次限流命中。resets_at 为解析到的绝对恢复时间(epoch)，解析不出则 None。"""

    resets_at: float | None = None


def detect(text: str) -> RateLimitHit | None:
    """从(已剥 ANSI 的)终端文本判断是否撞上限流。拿不准一律返回 None。

    反匹配是**局部抑制**而不是整段一票否决：剥掉 ANSI 后底栏提示会和正文粘到
    同一行（实测 `…hit your usage limit.Try again at …PM.Approaching rate limits`），
    整段否决会把真限流一起否掉。因此只有当某条 hit 短语**自身落在**某条反匹配
    的区间内时才抑制它；只要还剩一条未被覆盖的 hit，就判定为真限流。
    """
    if not text:
        return None
    veto_spans = [m.span() for p in _NOT_HIT_RES for m in p.finditer(text)]
    for pattern in _HIT_RES:
        for m in pattern.finditer(text):
            start, end = m.span()
            covered = any(vs <= start and end <= ve for vs, ve in veto_spans)
            if not covered:
                return RateLimitHit(resets_at=_parse_reset_at(text))
    return None


def _parse_reset_at(text: str) -> float | None:
    """抽 `try again at <日期 时间>`。解析失败返回 None，由调用方退回退避策略。

    刻意按**本地时区**解释：CLI 打印的就是用户本地时间。解析歪了不会致命——
    resume_after() 会把已经过去的时间丢弃、退回退避。
    """
    m = _RESET_AT_RE.search(text)
    if not m:
        return None
    mon = _MONTHS.get(m.group(1).lower())
    if not mon:
        return None
    try:
        day, year = int(m.group(2)), int(m.group(3))
        hour, minute = int(m.group(4)), int(m.group(5))
        if m.group(6).upper() == "PM" and hour != 12:
            hour += 12
        elif m.group(6).upper() == "AM" and hour == 12:
            hour = 0
        return datetime(year, mon, day, hour, minute).timestamp()
    except ValueError:
        return None


def backoff_seconds(attempt: int) -> float:
    """第 attempt 次(从 0 起)自动续跑前的等待：5min → 10min → 20min …，封顶 60min。"""
    if attempt < 0:
        attempt = 0
    if attempt > 20:  # 防 2**attempt 溢出成天文数字
        return BACKOFF_CAP_SECONDS
    return min(BACKOFF_BASE_SECONDS * (2 ** attempt), BACKOFF_CAP_SECONDS)


def resume_after(hit: RateLimitHit, attempt: int, now: float) -> float:
    """算出该在什么时刻自动续跑。

    优先用引擎自己给的恢复时间；没有、或那个时间已经过去(解析歪了/跨时区)
    则退回指数退避——绝不能返回一个「已经到点」的时间，那会变成疯狂重试。
    """
    if hit.resets_at is not None and hit.resets_at > now:
        return hit.resets_at
    return now + backoff_seconds(attempt)


def auto_resume_enabled() -> bool:
    """限流自动续跑总开关（默认开）。BOSUN_RATE_LIMIT_RESUME=0 关闭。"""
    return os.environ.get("BOSUN_RATE_LIMIT_RESUME", "1").strip().lower() not in _FALSE


def max_attempts() -> int:
    """单任务最多自动续跑次数（默认 3），超过转 failed，避免无限重试。"""
    try:
        value = int(os.environ.get("BOSUN_RATE_LIMIT_MAX_RETRY", "3"))
    except ValueError:
        return 3
    return max(0, value)
