import { type ReactNode } from "react";
import { createPortal } from "react-dom";

export function Modal({
  title,
  onClose,
  children,
  wide,
  xl,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  wide?: boolean;
  /** 更宽的弹窗（三栏并排等场景），优先于 wide。 */
  xl?: boolean;
}) {
  return createPortal(
    <div
      data-no-pull-refresh
      // 必须高于终端面板（z-[60]），否则在终端详情打开时弹窗会被压在下面。
      className="fixed inset-0 z-[65] flex h-full flex-col items-center justify-end overflow-hidden bg-slate-950/50 px-3 pt-[max(0.75rem,env(safe-area-inset-top))] pb-[max(0.75rem,env(safe-area-inset-bottom))] backdrop-blur-sm supports-[height:100dvh]:h-dvh sm:justify-center sm:px-4 sm:pt-[max(1rem,env(safe-area-inset-top))] sm:pb-[max(1rem,env(safe-area-inset-bottom))]"
      role="dialog"
      aria-modal="true"
      aria-label={title}
    >
      <div
        className={`flex min-h-0 max-h-full w-full flex-col ${xl ? "sm:max-w-[1080px]" : wide ? "sm:max-w-[720px]" : "sm:max-w-[480px]"} overflow-hidden rounded-2xl border border-dh-bsoft bg-dh-surface shadow-xl shadow-black/40 ring-1 ring-inset ring-white/[0.04]`}
      >
        {/* 标题栏留在滚动区外，长内容和安全区都不能把关闭按钮挤出屏幕。 */}
        <div className="flex shrink-0 items-center gap-3 p-4 sm:p-5">
          <h2 className="min-w-0 flex-1 break-words text-base font-semibold text-dh-text">{title}</h2>
          <button
            type="button"
            className="flex h-11 w-11 shrink-0 items-center justify-center rounded-md text-slate-400 hover:bg-dh-hover hover:text-slate-50"
            onClick={onClose}
            aria-label="关闭"
            title="关闭"
          >
            ✕
          </button>
        </div>
        <div className="min-h-0 overflow-y-auto overscroll-contain px-4 pb-4 sm:px-5 sm:pb-5">
          {children}
        </div>
      </div>
    </div>,
    document.body,
  );
}
