import { useEffect, useRef, useState, type ClipboardEvent as ReactClipboardEvent } from "react";
import { api } from "./api";
import { AttachmentPicker } from "./components/AttachmentPicker";

// ---- 全局 overlay 状态(模块级 + 订阅) ----
type Toast = {
  id: number;
  message: string;
  tone: "info" | "success" | "error";
  dedupeKey?: string;
};
type Dialog =
  | { kind: "confirm"; message: string; danger?: boolean; resolve: (v: boolean) => void }
  | {
      kind: "prompt";
      message: string;
      defaultValue: string;
      // 传了任务 id 才显示附件区：文件要上传到该任务目录，拿到服务器路径后才拼进指令
      attachToTaskId?: number;
      resolve: (v: string | null) => void;
    };

type PendingAttachment = { id: string; file: File; preview?: string };

let _toasts: Toast[] = [];
let _dialogs: Dialog[] = []; // 队列: 多个弹窗排队, 逐个显示(避免覆盖丢失 promise)
let _seq = 1;
const _subs = new Set<() => void>();
function _emit() {
  _subs.forEach((f) => f());
}

export function toast(message: string, tone: Toast["tone"] = "info", dedupeKey?: string) {
  // 状态事件可能短时间重复到达。同一业务通知可传稳定 key，避免在消失前铺满屏幕。
  if (dedupeKey && _toasts.some((item) => item.dedupeKey === dedupeKey)) return;
  const id = _seq++;
  _toasts = [..._toasts, { id, message, tone, dedupeKey }];
  _emit();
  setTimeout(() => {
    _toasts = _toasts.filter((t) => t.id !== id);
    _emit();
  }, 3800);
}

export function confirmDialog(message: string, opts?: { danger?: boolean }): Promise<boolean> {
  return new Promise((resolve) => {
    _dialogs = [..._dialogs, { kind: "confirm", message, danger: opts?.danger, resolve }];
    _emit();
  });
}

export function promptDialog(
  message: string,
  defaultValue = "",
  opts?: { attachToTaskId?: number },
): Promise<string | null> {
  return new Promise((resolve) => {
    _dialogs = [
      ..._dialogs,
      { kind: "prompt", message, defaultValue, attachToTaskId: opts?.attachToTaskId, resolve },
    ];
    _emit();
  });
}

// 没有附件时原样返回，"留空=只加载上下文继续"的语义不能被默认文案顶掉。
function promptWithAttachments(prompt: string, paths: string[]): string {
  if (paths.length === 0) return prompt;
  const base = prompt.trim() || "请查看附件，并根据附件内容继续。";
  return `${base}\n\n附件（本地路径，请查看）：\n${paths.map((path) => `- ${path}`).join("\n")}`;
}

function closeDialog(value: boolean | string | null) {
  const d = _dialogs[0];
  _dialogs = _dialogs.slice(1);
  _emit();
  if (d) (d.resolve as any)(value);
}

// ---- 宿主组件(在 App 挂一次) ----
export function OverlayHost() {
  const [, setV] = useState(0);
  useEffect(() => {
    const f = () => setV((x) => x + 1);
    _subs.add(f);
    return () => {
      _subs.delete(f);
    };
  }, []);

  const dlg = _dialogs[0] ?? null;
  const [promptVal, setPromptVal] = useState("");
  const [attachments, setAttachments] = useState<PendingAttachment[]>([]);
  const [uploading, setUploading] = useState(false);
  const previews = useRef<Set<string>>(new Set());
  const attachTaskId = dlg?.kind === "prompt" ? dlg.attachToTaskId : undefined;

  const dropPreviews = () => {
    previews.current.forEach((preview) => URL.revokeObjectURL(preview));
    previews.current.clear();
  };
  useEffect(() => dropPreviews, []);
  useEffect(() => {
    // 切到下一个排队弹窗时，上一个的待传附件不能跟着串过去
    if (dlg?.kind === "prompt") setPromptVal(dlg.defaultValue);
    dropPreviews();
    setAttachments([]);
    setUploading(false);
  }, [dlg]);

  function addAttachments(files: File[]) {
    if (files.length === 0) return;
    setAttachments((current) => {
      const known = new Set(current.map(({ file }) => `${file.name}:${file.size}:${file.lastModified}`));
      const additions = files
        .filter((file) => !known.has(`${file.name}:${file.size}:${file.lastModified}`))
        .map((file) => {
          const preview = file.type.startsWith("image/") ? URL.createObjectURL(file) : undefined;
          if (preview) previews.current.add(preview);
          return { id: `${Date.now()}-${Math.random()}`, file, preview };
        });
      return [...current, ...additions];
    });
  }

  function removeAttachment(id: string) {
    setAttachments((current) => current.filter((attachment) => {
      if (attachment.id !== id) return true;
      if (attachment.preview) {
        URL.revokeObjectURL(attachment.preview);
        previews.current.delete(attachment.preview);
      }
      return false;
    }));
  }

  function handlePaste(event: ReactClipboardEvent<HTMLElement>) {
    if (attachTaskId === undefined) return;
    const itemImages = Array.from(event.clipboardData.items)
      .filter((item) => item.kind === "file" && item.type.startsWith("image/"))
      .map((item) => item.getAsFile())
      .filter((file): file is File => file !== null);
    const pastedImages = itemImages.length > 0
      ? itemImages
      : Array.from(event.clipboardData.files).filter((file) => file.type.startsWith("image/"));
    if (pastedImages.length === 0) return;
    addAttachments(pastedImages);
    if (!event.clipboardData.getData("text/plain")) event.preventDefault();
  }

  async function submitPrompt() {
    if (uploading) return;
    if (attachTaskId === undefined || attachments.length === 0) {
      closeDialog(promptVal);
      return;
    }
    const target = _dialogs[0];
    setUploading(true);
    const paths: string[] = [];
    let failures = 0;
    for (const attachment of attachments) {
      try {
        const uploaded = await api.uploadFile(attachTaskId, attachment.file);
        paths.push(uploaded.path);
      } catch {
        failures += 1;
      }
    }
    setUploading(false);
    // 上传期间用户可能已取消：此时队首已换人，不能再拿别人的 dialog 兑现
    if (_dialogs[0] !== target) return;
    if (failures > 0) toast(`${failures} 个附件上传失败`, "error");
    // 全部失败且没写指令：留在弹窗里让用户重试，别把"只加载上下文"当成用户本意提交出去
    if (paths.length === 0 && !promptVal.trim()) return;
    closeDialog(promptWithAttachments(promptVal, paths));
  }

  const toneCls: Record<Toast["tone"], string> = {
    info: "border-slate-200 bg-white text-slate-700",
    success: "border-emerald-200 bg-emerald-50 text-emerald-700",
    error: "border-rose-200 bg-rose-50 text-rose-700",
  };

  return (
    <>
      {/* Toasts 右上角堆叠 */}
      <div className="pointer-events-none fixed left-4 right-4 top-[max(1rem,env(safe-area-inset-top))] z-[75] flex flex-col items-end gap-2 sm:left-auto">
        {_toasts.map((t) => (
          <div
            key={t.id}
            className={`pointer-events-auto max-w-sm rounded-lg border px-4 py-2.5 text-sm shadow-lg ${toneCls[t.tone]}`}
          >
            {t.message}
          </div>
        ))}
      </div>

      {/* 确认 / 输入 弹窗 */}
      {dlg && (
        <div
          data-no-pull-refresh
          className="fixed inset-0 z-[70] flex items-center justify-center bg-slate-900/30 px-4 pt-[max(1rem,env(safe-area-inset-top))] pb-[max(1rem,env(safe-area-inset-bottom))] backdrop-blur-sm"
          role="dialog"
          aria-modal="true"
        >
          <div
            className="max-h-full w-full max-w-[420px] overflow-auto rounded-xl border border-slate-200 bg-white p-5 shadow-xl"
            onPaste={handlePaste}
          >
            <div className="mb-3 flex items-start gap-3">
              <div className="min-w-0 flex-1 whitespace-pre-wrap text-sm text-slate-700">{dlg.message}</div>
              <button
                className="-mr-1 -mt-1 rounded-md px-1 text-slate-400 hover:bg-slate-100 hover:text-slate-600"
                onClick={() => closeDialog(dlg.kind === "prompt" ? null : false)}
                aria-label="关闭"
                title="关闭"
              >
                ✕
              </button>
            </div>
            {dlg.kind === "prompt" && (
              <input
                autoFocus
                className="mt-3 w-full rounded-lg border border-slate-200 bg-slate-50 px-2.5 py-1.5 text-sm text-slate-800 focus:border-teal-500 focus:outline-none"
                value={promptVal}
                onChange={(e) => setPromptVal(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    void submitPrompt();
                  }
                }}
              />
            )}
            {dlg.kind === "prompt" && attachTaskId !== undefined && (
              <div className="mt-3 rounded-lg border-2 border-dashed border-teal-500 bg-slate-900 p-3 shadow-inner shadow-black/20">
                <div className="flex items-center gap-2">
                  <span className="text-xs font-medium text-slate-100">📋 粘贴截图到这里（Ctrl/⌘ + V）</span>
                  <AttachmentPicker
                    accept="image/*"
                    buttonClassName="ml-auto rounded-md border border-teal-400 bg-teal-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-teal-500 disabled:opacity-50"
                    disabled={uploading}
                    onFiles={addAttachments}
                  >
                    + 图片
                  </AttachmentPicker>
                  <AttachmentPicker
                    buttonClassName="rounded-md border border-slate-500 bg-slate-800 px-2.5 py-1 text-xs font-medium text-slate-100 hover:bg-slate-700 disabled:opacity-50"
                    disabled={uploading}
                    onFiles={addAttachments}
                  >
                    + 文件
                  </AttachmentPicker>
                </div>
                {attachments.length > 0 && (
                  <div className="mt-2 flex flex-wrap gap-2">
                    {attachments.map((attachment) => (
                      attachment.preview ? (
                        <div key={attachment.id} className="group relative h-16 w-16 overflow-hidden rounded-md border-2 border-slate-500 bg-slate-800 shadow-sm">
                          <img src={attachment.preview} alt={attachment.file.name} className="h-full w-full object-cover" />
                          <button
                            type="button"
                            className="absolute right-0.5 top-0.5 rounded bg-rose-600 px-1.5 text-xs font-bold text-white shadow hover:bg-rose-500"
                            onClick={() => removeAttachment(attachment.id)}
                            aria-label={`移除 ${attachment.file.name}`}
                          >
                            ×
                          </button>
                        </div>
                      ) : (
                        <span
                          key={attachment.id}
                          className="inline-flex max-w-full items-center gap-1 rounded-md border border-slate-600 bg-slate-800 px-2 py-1 text-xs text-slate-200"
                        >
                          <span className="truncate">📎 {attachment.file.name}</span>
                          <button
                            type="button"
                            className="shrink-0 text-slate-400 hover:text-slate-200"
                            onClick={() => removeAttachment(attachment.id)}
                            aria-label={`移除 ${attachment.file.name}`}
                          >
                            ✕
                          </button>
                        </span>
                      )
                    ))}
                  </div>
                )}
              </div>
            )}
            <div className="mt-4 flex justify-end gap-2">
              <button
                className="rounded-lg border border-slate-200 px-3 py-1.5 text-sm text-slate-600 hover:bg-slate-50"
                onClick={() => closeDialog(dlg.kind === "prompt" ? null : false)}
              >
                取消
              </button>
              <button
                autoFocus={dlg.kind === "confirm"}
                disabled={uploading}
                className={`rounded-lg px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50 ${
                  dlg.kind === "confirm" && dlg.danger
                    ? "bg-rose-500 hover:bg-rose-600"
                    : "bg-teal-600 hover:bg-teal-700"
                }`}
                onClick={() => (dlg.kind === "prompt" ? void submitPrompt() : closeDialog(true))}
              >
                {uploading ? "上传中…" : "确定"}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
