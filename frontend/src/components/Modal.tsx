import { useLayoutEffect, useState, type CSSProperties, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { isCoarsePointer } from "../pointer";

function useModalViewport(): CSSProperties | undefined {
  const [bounds, setBounds] = useState<CSSProperties>();
  useLayoutEffect(() => {
    const viewport = window.visualViewport;
    if (!viewport || !isCoarsePointer()) return;
    let frame: number | undefined;
    const update = () => {
      frame = undefined;
      // 缩放交给浏览器；只跟随键盘与横竖屏切换，避免抵消用户主动放大。
      if (Math.abs(viewport.scale - 1) > 0.01) return;
      if (!Number.isFinite(viewport.height) || viewport.height < 160) return;
      setBounds({
        top: viewport.offsetTop,
        left: viewport.offsetLeft,
        width: viewport.width,
        height: viewport.height,
        right: "auto",
        bottom: "auto",
      });
    };
    const schedule = () => {
      if (frame === undefined) frame = window.requestAnimationFrame(update);
    };
    update();
    viewport.addEventListener("resize", schedule);
    viewport.addEventListener("scroll", schedule);
    window.addEventListener("resize", schedule);
    return () => {
      if (frame !== undefined) window.cancelAnimationFrame(frame);
      viewport.removeEventListener("resize", schedule);
      viewport.removeEventListener("scroll", schedule);
      window.removeEventListener("resize", schedule);
    };
  }, []);
  return bounds;
}

export function Modal({
  title,
  onClose,
  children,
  footer,
  wide,
  xl,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  /** 操作栏独立于表单滚动区，软键盘打开时仍可点击。 */
  footer?: ReactNode;
  wide?: boolean;
  /** 更宽的弹窗（三栏并排等场景），优先于 wide。 */
  xl?: boolean;
}) {
  const viewportStyle = useModalViewport();
  return createPortal(
    <div
      data-no-pull-refresh
      // 必须高于终端面板（z-[60]），否则在终端详情打开时弹窗会被压在下面。
      className="fixed inset-0 z-[65] overflow-hidden bg-slate-950/50 backdrop-blur-sm"
      role="dialog"
      aria-modal="true"
      aria-label={title}
    >
      <div
        className="absolute inset-0 flex flex-col items-center justify-end px-3 pt-[max(0.75rem,env(safe-area-inset-top))] pb-[max(0.75rem,env(safe-area-inset-bottom))] sm:justify-center sm:px-4 sm:pt-[max(1rem,env(safe-area-inset-top))] sm:pb-[max(1rem,env(safe-area-inset-bottom))]"
        style={viewportStyle}
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
          {footer && (
            <div className="shrink-0 border-t border-dh-bsoft px-4 py-3 sm:px-5">
              {footer}
            </div>
          )}
        </div>
      </div>
    </div>,
    document.body,
  );
}
