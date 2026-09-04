import { useEffect, useState } from "react";

import { api, type TaskFileKind } from "../api";

type Loaded = { kind: TaskFileKind; url: string; text: string | null; size: number };

const KIND_LABEL: Record<TaskFileKind, string> = {
  image: "图片",
  pdf: "PDF",
  text: "文本",
  binary: "二进制文件",
};

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

/** 终端里双击文件后的预览弹层。文件由后端代读，手机远程访问走的是同一条路径。 */
export function FilePreviewOverlay({
  taskId,
  path,
  onClose,
}: {
  taskId: number;
  path: string;
  onClose: () => void;
}) {
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let disposed = false;
    let objectUrl: string | null = null;
    setLoaded(null);
    setError(null);
    api
      .taskFile(taskId, path)
      .then(async ({ kind, blob }) => {
        if (disposed) return;
        // 文本自己读出来渲染，不交给浏览器解析——.html/.svg 内联渲染等于在应用同源下执行它
        const text = kind === "text" ? await blob.text() : null;
        if (disposed) return;
        objectUrl = URL.createObjectURL(blob);
        setLoaded({ kind, url: objectUrl, text, size: blob.size });
      })
      .catch((e: unknown) => {
        if (!disposed) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      disposed = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [taskId, path]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const name = path.split("/").pop() || path;

  return (
    <div
      className="fixed inset-0 z-[80] flex items-center justify-center bg-black/60 px-3 pt-[max(0.75rem,env(safe-area-inset-top))] pb-[max(0.75rem,env(safe-area-inset-bottom))] backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        className="flex max-h-full w-full max-w-4xl flex-col overflow-hidden rounded-xl border border-dh-bsoft bg-dh-surface shadow-xl shadow-black/50 ring-1 ring-inset ring-white/[0.04]"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2 border-b border-dh-bsoft px-4 py-2.5">
          <div className="min-w-0 flex-1">
            <div className="truncate text-sm font-medium text-dh-text" title={path}>
              {name}
            </div>
            <div className="truncate text-[11px] text-dh-muted" title={path}>
              {path}
              {loaded && ` · ${KIND_LABEL[loaded.kind]} · ${formatSize(loaded.size)}`}
            </div>
          </div>
          {loaded && (
            <a
              href={loaded.url}
              download={name}
              className="shrink-0 rounded-md border border-dh-border bg-dh-s2 px-2.5 py-1 text-xs text-slate-100 hover:bg-dh-hover"
            >
              下载
            </a>
          )}
          <button
            type="button"
            onClick={onClose}
            className="shrink-0 rounded-md px-2 py-1 text-sm text-dh-muted hover:bg-dh-hover hover:text-slate-50"
            title="关闭（Esc）"
          >
            ✕
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-auto bg-dh-soft">
          {error && (
            <div className="px-4 py-8 text-center text-sm text-rose-300">{error}</div>
          )}
          {!error && !loaded && (
            <div className="px-4 py-8 text-center text-sm text-dh-muted">加载中…</div>
          )}
          {loaded?.kind === "image" && (
            <div className="flex min-h-[8rem] items-center justify-center p-3">
              <img src={loaded.url} alt={name} className="max-h-[70vh] max-w-full object-contain" />
            </div>
          )}
          {loaded?.kind === "pdf" && (
            <iframe src={loaded.url} title={name} className="h-[70vh] w-full border-0 bg-dh-bg" />
          )}
          {loaded?.kind === "text" && (
            <pre className="max-h-[70vh] overflow-auto px-4 py-3 text-[12px] leading-5 text-dh-tsoft">
              {loaded.text}
            </pre>
          )}
          {loaded?.kind === "binary" && (
            <div className="px-4 py-8 text-center text-sm text-dh-muted">
              这个类型无法预览，用上方「下载」保存到本地。
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
