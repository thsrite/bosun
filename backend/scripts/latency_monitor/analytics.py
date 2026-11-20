"""Privacy-preserving OMP/CPA normalization and deliberately conservative joins."""
from __future__ import annotations

import hashlib
import hmac
import math
import re
from datetime import datetime, timedelta, timezone

_LOCAL_TZ = timezone(timedelta(hours=8))
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}\Z")
_ACCOUNT = re.compile(r"account-[0-9a-f]{12}\Z")
_STOPS = {"stop", "length", "toolUse", "tool_calls", "end_turn", "max_tokens",
          "error", "aborted", "cancelled", "canceled", "content_filter"}
_ROLES = {"user", "system", "developer", "assistant", "tool", "compaction",
          "branch_summary", "session", "model_change", "thinking_level_change"}
_ERRORS = {"tls_eof", "timeout", "rate_limit", "upstream_error", "cancelled", "unknown"}
_CALL_FIELDS = ("id", "start", "end", "duration_s", "ttft_s", "input_tokens",
                "cache_tokens", "output_tokens", "reasoning_tokens", "model", "api",
                "stop", "tool_names", "error_kind")


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _identifier(value):
    if isinstance(value, str) and _IDENTIFIER.fullmatch(value):
        return value
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 2**63 - 1:
        return value
    return None

def _tool_id(value):
    """Use a stable opaque key for long compound provider tool identifiers."""
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:|/-]{1,256}", value):
        return "tool-" + hashlib.sha256(value.encode("ascii")).hexdigest()[:32]
    return None



def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _tokens(value):
    if isinstance(value, int) and not isinstance(value, bool):
        return value if 0 <= value <= 2**63 - 1 else None
    if isinstance(value, str) and re.fullmatch(r"\d{1,19}", value):
        number = int(value)
        return number if number <= 2**63 - 1 else None
    number = _number(value)
    return int(number) if number is not None and number.is_integer() and number <= 2**63 - 1 else None


def _first(mapping, *keys):
    for key in keys:
        if mapping.get(key) is not None:
            return mapping[key]
    return None


def _timestamp(value, local=False, milliseconds=False):
    number = _number(value)
    if number is not None:
        return number / 1000 if milliseconds or number >= 100_000_000_000 else number
    if not isinstance(value, str) or len(value) > 80:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_LOCAL_TZ if local else timezone.utc)
        result = parsed.timestamp()
        return result if math.isfinite(result) and result >= 0 else None
    except (ValueError, OverflowError, OSError):
        return None


def _milliseconds(value):
    value = _number(value)
    return value / 1000 if value is not None else None


def _sum_tokens(*values):
    return sum(values) if all(value is not None for value in values) else None


def _error_kind(value):
    if not isinstance(value, str):
        return None
    lowered = value.lower()
    if re.search(r"unexpected eof|\beof\b", lowered):
        return "tls_eof"
    if re.search(r"timeout|timed out|deadline exceeded", lowered):
        return "timeout"
    if re.search(r"rate.?limit|too many requests|\b429\b", lowered):
        return "rate_limit"
    if re.search(r"cancelled|canceled|aborted", lowered):
        return "cancelled"
    if re.search(r"upstream.{0,40}(?:fail|error)|bad gateway|service unavailable|\b50[0-9]\b", lowered):
        return "upstream_error"
    return None


def session_event(record: dict) -> dict | None:
    """Discard session content, keeping only bounded identifiers and timing/usage."""
    record = _mapping(record)
    message = _mapping(record.get("message"))
    role = message.get("role")
    record_type = record.get("type")
    at = _timestamp(record.get("timestamp"))
    if role == "assistant":
        usage = _mapping(message.get("usage"))
        start = _timestamp(message.get("timestamp"), milliseconds=True)
        end = _timestamp(_first(message, "completedAt", "completed_at"))
        if end is None:
            end = _timestamp(_first(record, "completedAt", "completed_at", "timestamp"))
        duration = _milliseconds(_first(message, "duration", "durationMs", "duration_ms"))
        if duration is None:
            duration = _milliseconds(_first(usage, "duration", "durationMs", "duration_ms"))
        if duration is None and start is not None and end is not None and end >= start:
            duration = end - start
        if end is None and start is not None and duration is not None:
            end = _number(start + duration)
        ttft = _milliseconds(_first(message, "ttft", "ttftMs", "ttft_ms"))
        if ttft is None:
            ttft = _milliseconds(_first(usage, "ttft", "ttftMs", "ttft_ms"))
        cache_read = _tokens(usage.get("cacheRead"))
        cache_write = _tokens(usage.get("cacheWrite"))
        cache = _sum_tokens(cache_read, cache_write)
        input_tokens = _sum_tokens(_tokens(usage.get("input")), cache_read, cache_write)
        content = message.get("content")
        names = []
        for block in content if isinstance(content, list) else []:
            block = _mapping(block)
            name = _identifier(block.get("name"))
            if block.get("type") in ("toolCall", "tool_call") and isinstance(name, str) and name not in names:
                names.append(name)
        stop = message.get("stopReason")
        stop = stop if isinstance(stop, str) and stop in _STOPS else None
        error_kind = _error_kind(message.get("errorMessage"))
        if stop in ("aborted", "cancelled", "canceled"):
            error_kind = "cancelled"
        elif stop == "error" and error_kind is None:
            error_kind = "unknown"
        return {
            "kind": "call", "id": _identifier(record.get("id")), "start": start,
            "end": end, "duration_s": duration, "ttft_s": ttft,
            "input_tokens": input_tokens, "cache_tokens": cache,
            "output_tokens": _tokens(usage.get("output")),
            "reasoning_tokens": _tokens(_first(usage, "reasoning", "reasoningTokens", "reasoning_tokens")),
            "model": _identifier(message.get("model")), "api": _identifier(message.get("api")),
            "stop": stop, "tool_names": names, "error_kind": error_kind,
        }
    if role == "toolResult":
        return {"kind": "tool_end", "id": _tool_id(message.get("toolCallId")),
                "name": _identifier(_first(message, "toolName", "name")),
                "end": at if at is not None else _timestamp(message.get("timestamp")),
                "error": message.get("isError") is True}
    data = _mapping(record.get("data"))
    if record_type == "tool_execution_start" or (
        record_type == "custom" and record.get("customType") == "tool_execution_start"
    ):
        source = data if data else record
        return {"kind": "tool_start", "id": _tool_id(source.get("toolCallId")),
                "name": _identifier(_first(source, "toolName", "name")),
                "start": at if at is not None else _timestamp(source.get("timestamp"))}
    activity_role = role if isinstance(role, str) and role in _ROLES else record_type
    if isinstance(activity_role, str) and activity_role in _ROLES:
        return {"kind": "activity", "at": at if at is not None else _timestamp(message.get("timestamp")),
                "role": activity_role}
    return None


def _account(source, salt):
    if not isinstance(source, str) or not source.strip() or not isinstance(salt, bytes) or not salt:
        return None
    source = source.strip().replace("\\", "/").rsplit("/", 1)[-1]
    if source.lower().endswith(".json"):
        source = source[:-5]
        if source.lower().startswith("codex-"):
            source = source[6:]
        source = re.sub(r"-(?:free|plus|pro|team|business|enterprise)$", "", source, flags=re.I)
        if "@" in source:
            source = re.sub(r"^[0-9a-f]{8}-", "", source, flags=re.I)
    source = source.casefold()
    return "account-" + hmac.new(salt, source.encode("utf-8", errors="replace"), hashlib.sha256).hexdigest()[:12]


def usage_event(row: dict, salt: bytes) -> dict:
    """Normalize a CPA usage row; account identities leave RAM only as keyed hashes."""
    row = _mapping(row)
    failed = row.get("failed")
    failed = bool(failed) if isinstance(failed, (bool, int)) and failed in (0, 1) else None
    return {
        "id": _identifier(row.get("id")), "start": _timestamp(row.get("timestamp"), local=True),
        "model": _identifier(row.get("model")),
        "input_tokens": _tokens(row.get("input_tokens")),
        "output_tokens": _tokens(row.get("output_tokens")),
        "cache_tokens": _tokens(row.get("cache_tokens")),
        "reasoning_tokens": _tokens(row.get("reasoning_tokens")),
        "duration_s": _milliseconds(row.get("duration_ms")),
        "ttft_s": _milliseconds(row.get("ttft_ms")),
        "account": _account(row.get("source"), salt), "failed": failed,
    }


_LOG_PREFIX = re.compile(
    r"^\[?(?P<at>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:\d{2})?)\]?\s*(?P<rest>.*)$"
)
_STREAM = re.compile(r"^\[([A-Za-z0-9_.:-]{1,80})\]")
_MODEL = re.compile(r"\bmodel(?:_name)?\s*(?:[=:]\s*|\s+)[\"']?([A-Za-z0-9][A-Za-z0-9_.:-]{0,79})(?=[\s,\"'\])}]|$)", re.I)
_AUTH_FILE = re.compile(r"(?:^|[\s=:\"'(/])((?:codex-)[^\s\"'<>/\\]+\.json)(?=[\s\"'),;\]}]|$)", re.I)
_SOURCE = re.compile(r"\b(?:source|email|account)\s*[=:]\s*[\"']?([^\s\"',;\]}]+)", re.I)
_DURATION = re.compile(r"\b(?:duration(?:_ms)?|elapsed|took)\s*[=:]?\s*(\d+(?:\.\d+)?)\s*(ms|µs|us|s|m)?\b", re.I)


def log_event(line: str, salt: bytes) -> dict | None:
    """Read structured CPA log metadata, never retaining a log message or auth path."""
    if not isinstance(line, str):
        return None
    match = _LOG_PREFIX.match(line)
    if not match:
        return None
    at = _timestamp(match.group("at").replace(",", "."), local=True)
    if at is None:
        return None
    rest = match.group("rest")
    stream_match = _STREAM.match(rest)
    stream_id = _identifier(stream_match.group(1)) if stream_match else None
    if stream_id in ("debug", "info", "warn", "warning", "error", "trace") or (
        isinstance(stream_id, str) and not stream_id.strip("-0")
    ):
        stream_id = None
    model_match = _MODEL.search(rest)
    model = _identifier(model_match.group(1)) if model_match else None
    lowered = rest.lower()
    auth_match = _AUTH_FILE.search(rest)
    if ("oauth" in lowered or auth_match) and re.search(r"\b(?:select(?:ed|ing|ion)?|use|using|picked|choose|chosen)\b", lowered):
        source_match = _SOURCE.search(rest) if auth_match is None else None
        account = _account(auth_match.group(1) if auth_match else source_match.group(1) if source_match else None, salt)
        if account is None:
            return None
        return {"kind": "selection", "at": at, "stream_id": stream_id,
                "model": model, "account": account}
    error_kind = _error_kind(rest)
    if error_kind not in {"tls_eof", "timeout", "rate_limit", "upstream_error"}:
        return None
    duration_match = _DURATION.search(rest)
    duration = None
    if duration_match:
        duration = _number(duration_match.group(1))
        unit = duration_match.group(2)
        if duration is not None and unit:
            duration = _number(duration * {"ms": .001, "µs": .000001, "us": .000001, "s": 1, "m": 60}[unit.lower()])
        elif duration is not None and "duration_ms" in duration_match.group(0).lower():
            duration /= 1000
        else:
            duration = None
    return {"kind": "failure", "at": at, "stream_id": stream_id,
            "error_kind": error_kind, "duration_s": duration, "model": model}


def _clean_call(call):
    call = _mapping(call)
    clean = {"kind": "call"}
    for key in _CALL_FIELDS:
        value = call.get(key)
        if key in ("start", "end", "duration_s", "ttft_s"):
            clean[key] = _number(value)
        elif key.endswith("_tokens"):
            clean[key] = _tokens(value)
        elif key == "tool_names":
            clean[key] = [name for name in value if isinstance(name, str) and _identifier(name) is not None] if isinstance(value, list) else []
        elif key in ("stop", "error_kind"):
            allowed = _STOPS if key == "stop" else _ERRORS
            clean[key] = value if isinstance(value, str) and value in allowed else None
        else:
            clean[key] = _identifier(value)
    if "task_id" in call:
        clean["task_id"] = _tokens(call["task_id"])
    if "tool_errors" in call:
        clean["tool_errors"] = _tokens(call["tool_errors"])
    if "tool_duration_s" in call:
        clean["tool_duration_s"] = _number(call["tool_duration_s"])
    return clean


def _clean_usage(row):
    row = _mapping(row)
    account = row.get("account")
    return {"id": _identifier(row.get("id")), "start": _number(row.get("start")),
            "model": _identifier(row.get("model")),
            "input_tokens": _tokens(row.get("input_tokens")),
            "output_tokens": _tokens(row.get("output_tokens")),
            "cache_tokens": _tokens(row.get("cache_tokens")),
            "duration_s": _number(row.get("duration_s")), "ttft_s": _number(row.get("ttft_s")),
            "account": account if isinstance(account, str) and _ACCOUNT.fullmatch(account) else None,
            "failed": row.get("failed") if isinstance(row.get("failed"), bool) else None}


def enrich_calls(calls: list, usage: list, logs: list) -> list:
    """Join mutually unique candidates; output rate estimates the post-TTFT interval."""
    calls = [_clean_call(call) for call in calls if isinstance(call, dict)] if isinstance(calls, list) else []
    usage = [_clean_usage(row) for row in usage if isinstance(row, dict)] if isinstance(usage, list) else []
    logs = [row for row in logs if isinstance(row, dict)] if isinstance(logs, list) else []
    candidates = [[] for _ in calls]
    owners = [[] for _ in usage]
    token_keys = ("input_tokens", "output_tokens", "cache_tokens")
    usage_index = {}
    for row_index, row in enumerate(usage):
        key = (row["model"], *(row[field] for field in token_keys))
        if all(value is not None for value in key):
            usage_index.setdefault(key, []).append(row_index)
    for index, call in enumerate(calls):
        start, end = call["start"], call["end"]
        if start is None or end is None or end < start or call["model"] is None:
            continue
        if any(call[key] is None for key in token_keys):
            continue
        key = (call["model"], *(call[field] for field in token_keys))
        for row_index in usage_index.get(key, ()):
            row = usage[row_index]
            gateway_start = row["start"]
            if (gateway_start is not None and start - 2 <= gateway_start <= end
                    and abs(gateway_start - start) <= 30
                    and (row["duration_s"] is None or abs(gateway_start + row["duration_s"] - end) <= 5)):
                candidates[index].append(row_index)
                owners[row_index].append(index)
    result = []
    for index, call in enumerate(calls):
        call.update(gateway=None, output_rate=None, retry_count=None, retry_seconds=None,
                    correlation="unmatched", diagnosis=[])
        matches = candidates[index]
        row = None
        if matches:
            call["correlation"] = "ambiguous"
            if len(matches) == 1 and len(owners[matches[0]]) == 1:
                row = usage[matches[0]]
                call["correlation"] = "matched"
                call["gateway"] = {key: row[key] for key in ("id", "start", "duration_s", "ttft_s", "account", "failed")}
        duration = row["duration_s"] if row is not None else call["duration_s"]
        output_tokens = row["output_tokens"] if row is not None else call["output_tokens"]
        stream_ttft = row["ttft_s"] if row is not None else call["ttft_s"]
        stream_duration = duration - stream_ttft if (
            duration is not None and stream_ttft is not None and duration > stream_ttft
        ) else None
        if stream_duration is not None and output_tokens is not None:
            call["output_rate"] = _number(output_tokens / stream_duration)
        ttft = row["ttft_s"] if row is not None and row["ttft_s"] is not None else call["ttft_s"]
        if ttft is not None and ttft > 15:
            call["diagnosis"].append("high_ttft")
        if (output_tokens is not None and output_tokens >= 200 and stream_duration is not None
                and stream_duration >= 20 and call["output_rate"] is not None and call["output_rate"] < 15):
            call["diagnosis"].append("slow_stream_observed")
        if row is not None and row["account"] is not None:
            selections = [event for event in logs
                          if event.get("kind") == "selection" and event.get("model") == row["model"]
                          and event.get("account") == row["account"]
                          and _number(event.get("at")) is not None
                          and abs(_number(event["at"]) - row["start"]) <= 1.5]
            if len(selections) == 1:
                selection_at = _number(selections[0]["at"])
                selection_owners = [candidate for candidate in usage
                                    if candidate["model"] == row["model"] and candidate["account"] == row["account"]
                                    and candidate["start"] is not None
                                    and abs(candidate["start"] - selection_at) <= 1.5]
                stream = _identifier(selections[0].get("stream_id"))
                stream_selections = [event for event in logs if event.get("kind") == "selection"
                                     and event.get("stream_id") == stream
                                     and _number(event.get("at")) is not None
                                     and call["start"] - 2 <= _number(event["at"]) <= call["end"]]
                if stream is not None and len(selection_owners) == 1 and all(
                    event.get("model") == row["model"]
                    for event in stream_selections
                ):
                    failures = [event for event in logs if event.get("kind") == "failure"
                                and event.get("stream_id") == stream
                                and _number(event.get("at")) is not None
                                and call["start"] <= _number(event["at"]) <= call["end"]
                                and event.get("model") in (None, row["model"])]
                    call["retry_count"] = len(failures)
                    durations = [_number(event.get("duration_s")) for event in failures]
                    call["retry_seconds"] = _number(sum(durations)) if all(value is not None for value in durations) else None
                    if failures:
                        call["diagnosis"].append("gateway_retries_observed")
        result.append(call)
    return sorted(result, key=lambda call: call["start"] if call["start"] is not None else -1, reverse=True)
