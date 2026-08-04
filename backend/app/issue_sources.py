"""外部问题来源连接器：拉取外部条目 → finding（复用去重/收件箱管线）。

v1 仅 HTTP/REST。FETCHERS 注册表留好，Redis/数据库后续按同签名加。
"""
from __future__ import annotations

import base64
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import certifi

from . import db, events
from .services import discovery

_SSL_CTX = ssl.create_default_context(cafile=certifi.where())
_MAX_BYTES = 2_000_000  # 响应大小上限, 防拉爆内存
_MAX_ITEMS = 100        # 单次最多写多少条 finding
_MAX_ATTACHMENTS = 10
_MAX_ATTACHMENT_BYTES = 50_000_000
TIMEOUT = 20


def _dig(obj, path: str):
    """点路径取值; path 为空返回 obj 本身。"""
    if not path:
        return obj
    for part in path.split("."):
        if isinstance(obj, dict):
            obj = obj.get(part)
        else:
            return None
    return obj


def _auth_headers(cfg: dict) -> dict:
    t = cfg.get("auth_type") or "none"
    cred = cfg.get("auth_credential") or ""
    if not cred:
        return {}
    if t == "bearer":
        return {"Authorization": f"Bearer {cred}"}
    if t == "api_key":
        return {cfg.get("auth_header") or "X-API-Key": cred}
    if t == "basic":  # cred = "user:pass"
        return {"Authorization": "Basic " + base64.b64encode(cred.encode()).decode()}
    return {}


def _safe_filename(name: str) -> str:
    base = os.path.basename(name or "attachment")
    stem, ext = os.path.splitext(base)
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", stem) or "attachment"
    ext = re.sub(r"[^A-Za-z0-9.]", "", ext)[:20]
    return f"{stem[:120]}{ext}"


def _join_source_url(source_url: str, path: str) -> str:
    parsed = urllib.parse.urlparse(source_url)
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def _attachment_url(cfg: dict, att: dict) -> str:
    raw_url = att.get("url") or att.get("download_url")
    if raw_url:
        raw_url = str(raw_url)
        if raw_url.startswith(("http://", "https://")):
            return raw_url
        if raw_url.startswith("/"):
            return _join_source_url(cfg.get("url") or "", raw_url)

    filename = str(att.get("filename") or "").strip()
    if not filename:
        return ""
    quoted = urllib.parse.quote(filename)
    template = cfg.get("attachment_url_template")
    if isinstance(template, str) and "{filename}" in template:
        return template.replace("{filename}", quoted)

    # License Server bug integration API: /api/integration/bugs -> /api/integration/bug/file/{filename}
    source_url = str(cfg.get("url") or "")
    base = source_url.split("?", 1)[0]
    if "/bugs" in base:
        return base.split("/bugs", 1)[0].rstrip("/") + "/bug/file/" + quoted
    return ""


def _normalize_attachments(item: dict, cfg: dict) -> list[dict]:
    raw = item.get("attachments") if isinstance(item, dict) else None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = []
    if not isinstance(raw, list):
        return []

    out = []
    for att in raw[:_MAX_ATTACHMENTS]:
        if not isinstance(att, dict):
            continue
        filename = str(att.get("filename") or "").strip()
        name = str(att.get("name") or filename or "附件").strip()
        out.append({
            "type": str(att.get("type") or "file"),
            "filename": filename,
            "name": name,
            "url": _attachment_url(cfg, att),
        })
    return out


def fetch_http(cfg: dict) -> list[dict]:
    """发请求 → 按 items_path/字段映射 → [{title, detail, severity, attachments}]。"""
    url = (cfg.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise ValueError("url 必须以 http:// 或 https:// 开头")
    query = cfg.get("query")
    if isinstance(query, dict) and query:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(query)
    method = (cfg.get("method") or "GET").upper()
    headers = {"Accept": "application/json"}
    if isinstance(cfg.get("headers"), dict):
        headers.update({str(k): str(v) for k, v in cfg["headers"].items()})
    headers.update(_auth_headers(cfg))
    data = None
    if method == "POST" and cfg.get("body"):
        data = json.dumps(cfg["body"]).encode()
        headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method)
    # 直接赋值 req.headers 绕开 urllib 的 add_header ——后者会对头名做 str.capitalize()
    # (X-API-Key → X-api-key), 对大小写敏感的 API 会导致鉴权失败(401)
    req.headers = {str(k): str(v) for k, v in headers.items()}
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=_SSL_CTX) as r:
            raw = r.read(_MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        raise ValueError(f"HTTP {e.code}: {e.reason}")
    except (urllib.error.URLError, ssl.SSLError, TimeoutError) as e:
        raise ValueError(f"请求失败: {getattr(e, 'reason', e)}")
    if len(raw) > _MAX_BYTES:
        raise ValueError(f"响应过大(>{_MAX_BYTES} 字节)")
    try:
        payload = json.loads(raw.decode(errors="replace"))
    except json.JSONDecodeError:
        raise ValueError("响应不是合法 JSON")
    items = _dig(payload, cfg.get("items_path") or "")
    if not isinstance(items, list):
        raise ValueError(f"items_path='{cfg.get('items_path', '')}' 未定位到数组")
    tf = cfg.get("title_field") or "title"
    dfld = cfg.get("detail_field") or ""
    sev = cfg.get("severity") or "info"
    out = []
    for it in items[:_MAX_ITEMS]:
        title = it.get(tf) if isinstance(it, dict) else it
        detail = it.get(dfld) if (isinstance(it, dict) and dfld) else None
        title = (str(title) if title is not None else "").strip()[:200]
        if not title:
            continue
        if detail is not None and not isinstance(detail, str):
            detail = json.dumps(detail, ensure_ascii=False)
        out.append({
            "id": it.get("id") if isinstance(it, dict) else None,
            "title": title,
            "detail": (detail or "")[:4000],
            "severity": sev,
            "attachments": _normalize_attachments(it, cfg) if isinstance(it, dict) else [],
        })
    return out


FETCHERS = {"http": fetch_http}


def _fetch(type_: str, cfg: dict) -> list[dict]:
    fn = FETCHERS.get(type_)
    if fn is None:
        raise ValueError(f"不支持的来源类型: {type_}")
    return fn(cfg)


def test(type_: str, cfg: dict) -> dict:
    """连接测试: 只 fetch, 返回条数 + 前几条样例, 不写库。"""
    items = _fetch(type_, cfg)
    return {"count": len(items), "sample": items[:3]}


def pull(source_id: int) -> int:
    """拉取一次 → 写 finding(复用 discovery 去重/失败记忆)。返回新增数。"""
    src = db.query_one("SELECT * FROM issue_source WHERE id=?", (source_id,))
    if src is None:
        raise ValueError("来源不存在")
    cfg = json.loads(src["config"] or "{}")
    items = _fetch(src["type"], cfg)
    label = f"ext:{src['name']}"
    project = db.query_one("SELECT path FROM project WHERE id=?", (src["project_id"],))
    project_path = project["path"] if project else ""

    def _count() -> int:
        return db.query_one(
            "SELECT COUNT(*) c FROM finding WHERE project_id=? AND source=?",
            (src["project_id"], label),
        )["c"]

    before = _count()
    for idx, it in enumerate(items):
        detail = _detail_with_attachments(src, cfg, project_path, it, idx)
        _refresh_open_finding_detail(src["project_id"], label, it["title"], detail)
        discovery._add_finding(src["project_id"], label, it["severity"], it["title"], detail)
    db.execute("UPDATE issue_source SET last_pull_at=? WHERE id=?", (time.time(), source_id))
    events.emit("findings.updated", {"project_id": src["project_id"]})
    return _count() - before


def _refresh_open_finding_detail(project_id: int, source: str, title: str, detail: str) -> None:
    if not detail:
        return
    existing = db.query_one(
        "SELECT id, detail FROM finding WHERE project_id=? AND source=? AND title=? "
        "AND status IN ('open','muted')",
        (project_id, source, title),
    )
    if existing is None or (existing["detail"] or "") == detail:
        return
    db.execute("UPDATE finding SET detail=? WHERE id=?", (detail, existing["id"]))


def _download_attachment(cfg: dict, project_path: str, source_id: int, item_key: str, att: dict) -> str:
    url = att.get("url") or ""
    filename = _safe_filename(att.get("filename") or att.get("name") or "attachment")
    if not url or not project_path:
        return ""
    dest_dir = Path(project_path) / ".bosun-uploads" / "issue-sources" / str(source_id) / item_key
    _ensure_gitignored(Path(project_path), ".bosun-uploads/")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / filename
    if dest.is_file():
        return str(dest)

    headers = {"Accept": "*/*"}
    headers.update(_auth_headers(cfg))
    req = urllib.request.Request(str(url), method="GET")
    req.headers = {str(k): str(v) for k, v in headers.items()}
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=_SSL_CTX) as r:
        raw = r.read(_MAX_ATTACHMENT_BYTES + 1)
    if len(raw) > _MAX_ATTACHMENT_BYTES:
        raise ValueError(f"附件过大(>{_MAX_ATTACHMENT_BYTES} 字节): {att.get('name') or filename}")
    if not raw:
        raise ValueError(f"附件为空: {att.get('name') or filename}")
    dest.write_bytes(raw)
    return str(dest)


def _ensure_gitignored(project_path: Path, pattern: str) -> None:
    if not (project_path / ".git").exists():
        return
    gitignore = project_path / ".gitignore"
    try:
        text = gitignore.read_text() if gitignore.exists() else ""
        lines = {line.strip() for line in text.splitlines()}
        if pattern.strip() in lines:
            return
        prefix = "" if not text or text.endswith("\n") else "\n"
        gitignore.write_text(f"{text}{prefix}{pattern}\n")
    except OSError:
        pass


def _detail_with_attachments(src, cfg: dict, project_path: str, item: dict, index: int) -> str:
    detail = (item.get("detail") or "").strip()
    attachments = item.get("attachments") or []
    if not attachments:
        return detail

    raw_key = item.get("id") if item.get("id") is not None else index
    item_key = _safe_filename(str(raw_key))
    lines = ["附件:"]
    for att in attachments:
        label = att.get("name") or att.get("filename") or "附件"
        kind = att.get("type") or "file"
        parts = [f"- [{kind}] {label}"]
        if att.get("filename"):
            parts.append(f"文件名: {att['filename']}")
        try:
            local_path = _download_attachment(cfg, project_path, src["id"], item_key, att)
        except (OSError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ssl.SSLError, ValueError) as exc:
            local_path = ""
            parts.append(f"下载失败: {exc}")
        if local_path:
            parts.append(f"本地路径: {local_path}")
        elif att.get("url"):
            parts.append(f"下载地址: {att['url']}")
        lines.append("; ".join(parts))

    attach_detail = "\n".join(lines)
    if detail and detail != (item.get("title") or "").strip():
        return f"{detail}\n\n{attach_detail}"
    return attach_detail
