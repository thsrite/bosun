import { useEffect, useState } from "react";

// ---- 全局 overlay 状态(模块级 + 订阅) ----
type Toast = {
  id: number;
  message: string;
  tone: "info" | "success" | "error";
  dedupeKey?: string;
};
type Dialog =
  | { kind: "confirm"; message: string; danger?: boolean; resolve: (v: boolean) => void }
  | { kind: "prompt"; message: string; defaultValue: string; resolve: (v: string | null) => void };

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

export function promptDialog(message: string, defaultValue = ""): Promise<string | null> {
  return new Promise((resolve) => {
    _dialogs = [..._dialogs, { kind: "prompt", message, defaultValue, resolve }];
    _emit();
  });
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
  useEffect(() => {
    if (dlg?.kind === "prompt") setPromptVal(dlg.defaultValue);
  }, [dlg]);

  const toneCls: Record<Toast["tone"], string> = {
    info: "border-slate-200 bg-white text-slate-700",
    success: "border-emerald-200 bg-emerald-50 text-emerald-700",
    error: "border-rose-200 bg-rose-50 text-rose-700",
  };

  return (
    <>
      {/* Toasts 右上角堆叠 */}
      <div className="pointer-events-none fixed left-4 right-4 top-[max(1rem,env(safe-area-inset-top))] z-[60] flex flex-col items-end gap-2 sm:left-auto">
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
          <div className="max-h-full w-full max-w-[420px] overflow-auto rounded-xl border border-slate-200 bg-white p-5 shadow-xl">
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
                    closeDialog(promptVal);
                  }
                }}
              />
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
                className={`rounded-lg px-3 py-1.5 text-sm font-medium text-white ${
                  dlg.kind === "confirm" && dlg.danger
                    ? "bg-rose-500 hover:bg-rose-600"
                    : "bg-teal-600 hover:bg-teal-700"
                }`}
                onClick={() => closeDialog(dlg.kind === "prompt" ? promptVal : true)}
              >
                确定
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
