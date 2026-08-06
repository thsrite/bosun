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
      className="fixed inset-0 z-[65] flex items-end justify-center overflow-y-auto bg-slate-900/30 px-3 pt-[max(0.75rem,env(safe-area-inset-top))] pb-[max(0.75rem,env(safe-area-inset-bottom))] backdrop-blur-sm sm:items-center sm:p-4"
      role="dialog"
      aria-modal="true"
      aria-label={title}
    >
      <div
        className={`w-full ${xl ? "sm:max-w-[1080px]" : wide ? "sm:max-w-[720px]" : "sm:max-w-[480px]"} max-h-[calc(100vh-1.5rem)] max-h-[calc(100dvh-1.5rem)] overflow-auto overscroll-contain rounded-2xl border border-slate-200 bg-white p-4 pb-[max(1rem,env(safe-area-inset-bottom))] shadow-xl sm:p-5`}
      >
        <div className="mb-4 flex items-center">
          <h2 className="text-base font-semibold text-slate-900">{title}</h2>
          <button
            className="ml-auto rounded-md px-1 text-slate-400 hover:bg-slate-100 hover:text-slate-600"
            onClick={onClose}
            aria-label="关闭"
            title="关闭"
          >
            ✕
          </button>
        </div>
        {children}
      </div>
    </div>,
    document.body,
  );
}
