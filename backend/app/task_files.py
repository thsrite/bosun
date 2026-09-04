"""终端里双击文件时的预览取文件：路径解析与类型判定。

终端输出里的路径是**模型和命令行打印出来的任意字符串**，等于外部输入：远程访问时
一条 `cat /etc/passwd` 的回显就能变成一个可点的读文件请求。因此这里只放行任务自己
工作目录内的文件，且以 realpath 为准——软链接指向目录外同样拒绝。
"""
from __future__ import annotations

import mimetypes
from pathlib import Path

# 预览用途的单文件上限：再大就不是"看一眼"了，也别把内存喂给一次误点
MAX_PREVIEW_BYTES = 25 * 1024 * 1024

PreviewKind = str  # "image" | "pdf" | "text" | "binary"

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif", ".bmp", ".ico"}
_TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".json", ".jsonl", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".conf", ".env", ".csv", ".tsv", ".xml", ".html", ".htm",
    ".css", ".scss", ".less", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".py",
    ".rb", ".go", ".rs", ".java", ".kt", ".swift", ".c", ".h", ".cc", ".cpp", ".hpp",
    ".m", ".mm", ".sh", ".bash", ".zsh", ".fish", ".sql", ".diff", ".patch", ".lock",
    ".gradle", ".properties", ".tf", ".proto", ".graphql", ".vue", ".svelte",
}
# 常见的无扩展名文本文件
_TEXT_NAMES = {
    "dockerfile", "makefile", "license", "licence", "readme", "changelog", "notice",
    "procfile", "caddyfile", "justfile", "rakefile", "gemfile", "brewfile",
    ".gitignore", ".gitattributes", ".dockerignore", ".editorconfig", ".npmrc",
    ".nvmrc", ".prettierrc", ".eslintrc", ".env",
}


class TaskFileError(Exception):
    """带 HTTP 状态码的取文件失败；路由层原样转成 HTTPException。"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def resolve_task_file(root: Path, requested: str) -> Path:
    """把终端里点到的路径解析成工作目录内的真实文件；越界/不存在/过大都抛 TaskFileError。"""
    text = (requested or "").strip()
    if not text:
        raise TaskFileError(400, "路径为空")

    root = root.resolve()
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve()
    except OSError as exc:  # 循环软链接等
        raise TaskFileError(400, "路径无法解析") from exc

    # 用 relative_to 而不是字符串前缀：/work-secrets 与 /work 前缀相同但不是子目录。
    # resolve() 已展开软链接，所以指向目录外的链接在这里就会被拦下。
    if resolved != root and root not in resolved.parents:
        raise TaskFileError(403, "只能预览任务工作目录内的文件")
    if not resolved.is_file():
        raise TaskFileError(404, "文件不存在")
    if resolved.stat().st_size > MAX_PREVIEW_BYTES:
        raise TaskFileError(413, "文件过大，无法预览")
    return resolved


def preview_kind(path: Path) -> PreviewKind:
    """按扩展名判定前端该用哪种方式展示。"""
    suffix = path.suffix.lower()
    if suffix in _IMAGE_EXTS:
        return "image"
    if suffix == ".pdf":
        return "pdf"
    if suffix in _TEXT_EXTS:
        return "text"
    if not suffix and path.name.lower() in _TEXT_NAMES:
        return "text"
    if suffix and path.name.lower() in _TEXT_NAMES:  # .gitignore 之类，suffix 即全名
        return "text"
    return "binary"


def response_media_type(path: Path, kind: PreviewKind) -> str:
    """响应用的 Content-Type。

    文本一律按 text/plain 返回：.html/.svg 若按自身 MIME 内联返回，就是在应用同源下
    执行任务目录里的任意脚本——而那些文件正是 agent 刚写出来的。
    """
    if kind == "text":
        return "text/plain; charset=utf-8"
    if kind == "pdf":
        return "application/pdf"
    if kind == "image":
        guessed, _ = mimetypes.guess_type(path.name)
        if guessed and guessed.startswith("image/"):
            return guessed
        return "application/octet-stream"
    return "application/octet-stream"
