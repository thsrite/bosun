import { ClipboardAddon } from "@xterm/addon-clipboard";
import { FitAddon } from "@xterm/addon-fit";
import { WebLinksAddon } from "@xterm/addon-web-links";
import { Terminal } from "@xterm/xterm";
import {
  useCallback,
  useEffect,
  useReducer,
  useRef,
  useState,
  type CSSProperties,
  type TouchEvent,
} from "react";
import { api } from "../api";
import { WS_UNAUTHORIZED, setToken, wsProtocols } from "../auth";
import { AttachmentPicker } from "./AttachmentPicker";
import { ChatView } from "./ChatView";
import { SessionHistoryView } from "./SessionHistoryView";
import { confirmDialog, promptDialog, toast } from "../overlay";
import { guardQuota } from "../quota";
import { TERMINAL_SUBMIT_KEY } from "../terminalInput";
import { installHardWrappedWebLinkProvider } from "../terminalLinks";
import { STATUS_STYLE, taskStatusStyleKey } from "../theme";
import type { Engine, ReplySuggestion, Task } from "../types";
import { useSingleFlight } from "../useSingleFlight";
import { taskPromptText } from "../taskText";
import { engineShort } from "../engines";

// 「其他任务需要处理」提醒：同一轮等待只弹一次，关闭或跳转后不再打扰。
// 用 任务id + 该轮等待起点 作 key（waiting_since 在任务离开 waiting_input 时清空，
// 下一轮等待会换新值，所以是新一轮才会再弹）。放模块级是因为面板在任务间
// 切换时会按 key 重挂载，组件内 state 会丢。
const seenAttention = new Set<string>();

function attentionKey(t: Task): string {
  return `${t.id}:${t.waiting_since ?? 0}`;
}

function fmtTime(t: number | null): string {
  if (!t) return "—";
  return new Date(t * 1000).toLocaleString("zh", { hour12: false });
}

function fmtDur(start: number | null, end: number | null, accum = 0): string {
  const cur = start ? Math.max(0, Math.round((end ?? Date.now() / 1000) - start)) : 0;
  const secs = cur + (accum || 0); // accum: 续聊累计的历次时长
  if (!start && !accum) return "—";
  if (secs < 60) return `${secs}s`;
  const m = Math.floor(secs / 60);
  const s = secs % 60;
  if (m < 60) return `${m}m${s}s`;
  return `${Math.floor(m / 60)}h${m % 60}m`;
}

// 面板上下边钉在可视区域内（键盘弹出时底边=键盘上方可见区），保证是「确定高度」。
// 不再用 translateY 跟随 offsetTop——那是之前「页面经常跳」的根源；只更新边界，稳定不抖。

function isTouchDevice(): boolean {
  return (window.matchMedia?.("(pointer: coarse)").matches ?? false) || navigator.maxTouchPoints > 0;
}

function getLayoutViewportHeight(): number {
  return Math.max(window.innerHeight || 0, document.documentElement.clientHeight || 0);
}

function getMinUsableViewportHeight(): number {
  return Math.min(260, Math.max(160, Math.round(getLayoutViewportHeight() * 0.35)));
}

function preferredAudioMimeType(): string {
  if (typeof MediaRecorder === "undefined") return "";
  for (const type of ["audio/mp4", "audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus"]) {
    if (MediaRecorder.isTypeSupported(type)) return type;
  }
  return "";
}

function audioExtensionForMimeType(mimeType: string): string {
  if (mimeType.includes("mp4")) return "m4a";
  if (mimeType.includes("ogg")) return "ogg";
  if (mimeType.includes("webm")) return "webm";
  return "audio";
}

function hasFocusedTextInput(): boolean {
  const active = document.activeElement;
  return (
    active instanceof HTMLInputElement ||
    active instanceof HTMLTextAreaElement ||
    (active instanceof HTMLElement && active.isContentEditable)
  );
}

const PANEL_SAFE_AREA_STYLE = {
  paddingTop: "env(safe-area-inset-top)",
} satisfies CSSProperties;
const AUDIO_RECORDING_TIMEOUT_MS = 60000;
const MAX_DEFERRED_TERMINAL_BYTES = 4 * 1024 * 1024;
// 触摸滚动过历史后「回到最新」时，向 TUI 补发的滚轮步数与每帧步数。CC 一侧把滚轮当
// 菜单/列表选择处理，同步灌一大批会误触命令，所以总量克制、按帧摊开。
const APPLICATION_SCROLL_STEPS = 48;
const APPLICATION_SCROLL_STEPS_PER_FRAME = 8;
const APPLICATION_SCROLL_COOLDOWN_MS = 500;
const TERMINAL_THEME = {
  background: "#101722",
  foreground: "#e5edf7",
  cursor: "#f8fafc",
  cursorAccent: "#101722",
  selectionBackground: "#334155",
  black: "#1f2937",
  red: "#f87171",
  green: "#34d399",
  yellow: "#fbbf24",
  blue: "#60a5fa",
  magenta: "#c084fc",
  cyan: "#22d3ee",
  white: "#e5e7eb",
  brightBlack: "#64748b",
  brightRed: "#fb7185",
  brightGreen: "#6ee7b7",
  brightYellow: "#fde68a",
  brightBlue: "#93c5fd",
  brightMagenta: "#d8b4fe",
  brightCyan: "#67e8f9",
  brightWhite: "#f8fafc",
};
const ALT_BUFFER_MODES = new Set([47, 1047, 1049]);

function terminalDataByteLength(data: string | Uint8Array): number {
  return typeof data === "string" ? new TextEncoder().encode(data).byteLength : data.byteLength;
}

function isOnlyAltBufferModeParams(params: (number | number[])[]): boolean {
  if (params.length === 0) return false;
  return params.every((param) => typeof param === "number" && ALT_BUFFER_MODES.has(param));
}

function installAltBufferScrollbackWorkaround(term: Terminal): () => void {
  const setMode = term.parser.registerCsiHandler({ prefix: "?", final: "h" }, (params) =>
    isOnlyAltBufferModeParams(params),
  );
  const resetMode = term.parser.registerCsiHandler({ prefix: "?", final: "l" }, (params) =>
    isOnlyAltBufferModeParams(params),
  );
  return () => {
    setMode.dispose();
    resetMode.dispose();
  };
}

type XtermCoreInternals = {
  _inputEvent?: (ev: InputEvent) => boolean;
  _keyPressHandled?: boolean;
  _unprocessedDeadKey?: boolean;
  cancel?: (ev: Event, force?: boolean) => boolean | undefined;
  coreService?: { triggerDataEvent?: (data: string, wasUserInput?: boolean) => void };
  optionsService?: { rawOptions?: { screenReaderMode?: boolean } };
};

// 移动端输入法打不出符号的修复（与上游 xterm.js#5887 同根因，官方仅提议方向未发版）：
// 软键盘/IME 对每个键都报 keyCode 229，符号既不走 composition 也不触发 keypress，
// 只以 input(insertText) 事件到达；xterm 的 _inputEvent 用「本轮见过 keydown」当门闩
// 把它丢弃，keydown 229 路径的 textarea 差值兜底又因值累积、input 先于 keydown 的
// 顺序差异失准。这里把门闩收窄成「不在合成中」：insertText 直接发数据并阻止其落入
// textarea。_keyPressHandled 检查保留，键盘 keypress 正常路径不会双发；中文合成期间
// 的 insertCompositionText 不匹配 insertText，仍走 xterm 原生合成流程。
// 依赖 _core 私有成员：升级 xterm 后若成员缺失则整体跳过，回退原生行为。
function installMobileImeInsertTextFix(term: Terminal): void {
  const core = (term as unknown as { _core?: XtermCoreInternals })._core;
  if (
    !core ||
    typeof core._inputEvent !== "function" ||
    typeof core.cancel !== "function" ||
    typeof core.coreService?.triggerDataEvent !== "function"
  ) {
    return;
  }
  const original = core._inputEvent.bind(core);
  core._inputEvent = (ev: InputEvent): boolean => {
    if (
      ev.data &&
      ev.inputType === "insertText" &&
      !ev.isComposing &&
      !core.optionsService?.rawOptions?.screenReaderMode
    ) {
      if (core._keyPressHandled) return false;
      core._unprocessedDeadKey = false;
      core.coreService!.triggerDataEvent!(ev.data, true);
      core.cancel!(ev);
      return true;
    }
    return original(ev);
  };
}

function useVisualViewportCssVars() {
  useEffect(() => {
    const vv = window.visualViewport;
    const isTouch = window.matchMedia?.("(pointer: coarse)").matches ?? false;
    if (!vv || !isTouch) return;

    const rootStyle = document.documentElement.style;
    let frame: number | null = null;
    let lastHeight = "";

    const apply = () => {
      frame = null;
      const h = Math.round(vv.height);
      // iOS 听写/键盘切换时 visualViewport.height 会短暂报出极小值；写入后面板会被压成白屏。
      if (!Number.isFinite(h) || h < getMinUsableViewportHeight()) return;
      const height = `${h}px`;
      const layoutHeight = getLayoutViewportHeight();
      const bottomInset = Math.max(0, Math.round(layoutHeight - vv.offsetTop - h));
      const keyboardOpen = hasFocusedTextInput() && bottomInset > 80;
      if (height !== lastHeight) {
        rootStyle.setProperty("--dh-visual-viewport-height", height);
        lastHeight = height;
      }
      rootStyle.setProperty("--dh-panel-bottom-offset", keyboardOpen ? `${bottomInset}px` : "0px");
      rootStyle.setProperty(
        "--dh-panel-safe-bottom",
        keyboardOpen ? "0px" : "env(safe-area-inset-bottom)",
      );
    };
    const schedule = () => {
      if (frame == null) frame = window.requestAnimationFrame(apply);
    };

    schedule();
    vv.addEventListener("resize", schedule);
    window.addEventListener("orientationchange", schedule);
    return () => {
      if (frame != null) cancelAnimationFrame(frame);
      vv.removeEventListener("resize", schedule);
      window.removeEventListener("orientationchange", schedule);
      rootStyle.removeProperty("--dh-visual-viewport-height");
      rootStyle.removeProperty("--dh-panel-bottom-offset");
      rootStyle.removeProperty("--dh-panel-safe-bottom");
    };
  }, []);
}

function usePwaResumeViewportRecovery() {
  useEffect(() => {
    if (!isTouchDevice()) return;
    const rootStyle = document.documentElement.style;
    const timers = new Set<number>();

    const apply = () => {
      const raw = Math.round(window.visualViewport?.height ?? 0);
      const fallback = getLayoutViewportHeight();
      const h = Number.isFinite(raw) && raw >= getMinUsableViewportHeight() ? raw : fallback;
      if (Number.isFinite(h) && h > 0) {
        rootStyle.setProperty("--dh-visual-viewport-height", `${h}px`);
        const bottomInset = Math.max(
          0,
          Math.round(fallback - (window.visualViewport?.offsetTop ?? 0) - h),
        );
        const keyboardOpen = hasFocusedTextInput() && bottomInset > 80;
        rootStyle.setProperty("--dh-panel-bottom-offset", keyboardOpen ? `${bottomInset}px` : "0px");
        rootStyle.setProperty(
          "--dh-panel-safe-bottom",
          keyboardOpen ? "0px" : "env(safe-area-inset-bottom)",
        );
      }
      window.scrollTo(0, 0);
    };

    const schedule = () => {
      for (const delay of [0, 80, 250, 700]) {
        const timer = window.setTimeout(() => {
          timers.delete(timer);
          window.requestAnimationFrame(apply);
        }, delay);
        timers.add(timer);
      }
    };

    const onVisibility = () => {
      if (document.visibilityState === "visible") schedule();
    };

    window.addEventListener("pageshow", schedule);
    window.addEventListener("focus", schedule);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      for (const timer of timers) window.clearTimeout(timer);
      window.removeEventListener("pageshow", schedule);
      window.removeEventListener("focus", schedule);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, []);
}

// 移动端面板打开时锁死文档滚动。不要用 body position: fixed；iOS PWA 从第三方输入法
// 跳回时容易把 fixed body 恢复到错误位置，表现为白屏/内容顶出视口。
function useLockBodyScrollOnTouch() {
  useEffect(() => {
    if (!isTouchDevice()) return;
    const html = document.documentElement;
    const body = document.body;
    const scrollY = window.scrollY;
    const prev = {
      htmlOverflow: html.style.overflow,
      htmlOverscrollBehavior: html.style.overscrollBehavior,
      bodyOverflow: body.style.overflow,
      bodyOverscrollBehavior: body.style.overscrollBehavior,
    };
    html.style.overflow = "hidden";
    html.style.overscrollBehavior = "none";
    body.style.overflow = "hidden";
    body.style.overscrollBehavior = "none";
    return () => {
      html.style.overflow = prev.htmlOverflow;
      html.style.overscrollBehavior = prev.htmlOverscrollBehavior;
      body.style.overflow = prev.bodyOverflow;
      body.style.overscrollBehavior = prev.bodyOverscrollBehavior;
      window.scrollTo(0, scrollY);
    };
  }, []);
}

function useTerminalPanelTopOffset(): number {
  const [topOffset, setTopOffset] = useState(0);

  useEffect(() => {
    const desktopQuery = window.matchMedia("(min-width: 768px)");
    const header = document.querySelector<HTMLElement>(".dh-app-header");
    const update = () => {
      setTopOffset(desktopQuery.matches ? Math.round(header?.getBoundingClientRect().height ?? 0) : 0);
    };
    update();

    const resizeObserver =
      header && typeof ResizeObserver !== "undefined" ? new ResizeObserver(update) : null;
    if (header) resizeObserver?.observe(header);
    desktopQuery.addEventListener?.("change", update);
    window.addEventListener("resize", update);
    return () => {
      resizeObserver?.disconnect();
      desktopQuery.removeEventListener?.("change", update);
      window.removeEventListener("resize", update);
    };
  }, []);

  return topOffset;
}

function terminalPanelViewportStyle(topOffset: number): CSSProperties {
  const offset = `${topOffset}px`;
  return {
    top: offset,
    // 遮罩始终铺到屏幕真正底部：即便第三方输入法上报的键盘高度在键盘上方留出一条缝，
    // 这层 backdrop 也盖住它并拦掉落到后面页面（如任务列表）的点击。避让键盘改由面板高度承担。
    bottom: 0,
    marginTop: 0,
  };
}

// 面板顶部安全区 + 让出键盘高度：面板底边落在键盘顶部，输入条贴键盘，
// 下方那条键盘高度的缝留给全屏遮罩，不再暴露后面的页面。
function terminalPanelBodyStyle(): CSSProperties {
  return {
    ...PANEL_SAFE_AREA_STYLE,
    height: "calc(100% - var(--dh-panel-bottom-offset, 0px))",
    maxHeight: "calc(100% - var(--dh-panel-bottom-offset, 0px))",
  };
}

/** 内层：xterm + WebSocket（实时流 / 结束后回放日志），断线自动重连。 */
function TerminalView({
  taskId,
  live,
  suggestion,
  onSmartGenerate,
  smartLoading,
}: {
  taskId: number;
  live: boolean;
  suggestion?: ReplySuggestion | null;
  onSmartGenerate?: () => void;
  smartLoading?: boolean;
}) {
  const elRef = useRef<HTMLDivElement>(null);
  const termRef = useRef<Terminal | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const flushDeferredWritesRef = useRef<((onFlushed?: () => void) => void) | null>(null);
  const stickRef = useRef(true);
  const userScrolledRef = useRef(false);
  // 最近一次用户手势滚动的时间戳，用于区分"用户手动回到底部"与"Codex 原地重绘自发到底部"
  const lastUserScrollAtRef = useRef(0);
  const [atBottom, setAtBottom] = useState(true);
  const [disconnected, setDisconnected] = useState(false);
  const [connected, setConnected] = useState(false);
  const [pastingImages, setPastingImages] = useState(false);
  const liveRef = useRef(live);
  liveRef.current = live;

  const scrollToLatest = useCallback(() => {
    const term = termRef.current;
    if (!term) return;
    userScrolledRef.current = false;
    stickRef.current = true;
    const flush = flushDeferredWritesRef.current;
    const finish = () => {
      if (termRef.current !== term) return;
      term.scrollToBottom();
      setAtBottom(true);
    };
    if (flush) flush(finish);
    else finish();
  }, []);

  const sendTerminalData = useCallback(
    (data: string) => {
      const ws = wsRef.current;
      if (ws?.readyState !== WebSocket.OPEN) return;
      ws.send(data);
      scrollToLatest();
    },
    [scrollToLatest],
  );

  const pasteTerminalData = useCallback(
    (data: string) => {
      const term = termRef.current;
      const ws = wsRef.current;
      if (!term || ws?.readyState !== WebSocket.OPEN) return;
      // Route composed payloads (reply suggestion, uploaded file paths) through
      // xterm's paste API. xterm normalizes newlines and, when the TUI negotiated
      // bracketed paste, wraps the payload before onData forwards it to the PTY.
      // This prevents Codex's paste-burst detector from holding a lone ASCII key
      // (notably numbered choices such as "1") when Enter follows immediately.
      term.paste(data);
      scrollToLatest();
    },
    [scrollToLatest],
  );

  useEffect(() => {
    if (!elRef.current) return;
    const terminalHost = elRef.current;
    userScrolledRef.current = false;
    stickRef.current = true;
    setAtBottom(true);
    const term = new Terminal({
      fontSize: 12,
      fontFamily: "ui-monospace, Menlo, monospace",
      theme: TERMINAL_THEME,
      cursorBlink: true,
      convertEol: true,
      scrollback: 100000,
    });
    const fit = new FitAddon();
    // Claude/Codex TUIs copy application-managed mouse selections via OSC 52. Without this
    // handler they can render "copied ... chars" while the browser clipboard stays unchanged.
    const clipboard = new ClipboardAddon();
    const openWebLink = (_event: MouseEvent, uri: string) => {
      try {
        const url = new URL(uri);
        if (url.protocol !== "http:" && url.protocol !== "https:") return;
        window.open(url.href, "_blank", "noopener,noreferrer");
      } catch {
        // Ignore malformed terminal output instead of passing it to window.open.
      }
    };
    // Register first so a full URL split by a TUI's hard newline wins over the standard
    // provider's partial first-line match. WebLinksAddon remains the fallback for normal links.
    const hardWrappedLinks = installHardWrappedWebLinkProvider(term, openWebLink);
    const webLinks = new WebLinksAddon(openWebLink);
    term.loadAddon(fit);
    term.loadAddon(clipboard);
    term.loadAddon(webLinks);
    term.open(elRef.current);
    termRef.current = term;
    const disposeAltBufferWorkaround = installAltBufferScrollbackWorkaround(term);
    if (isTouchDevice()) installMobileImeInsertTextFix(term);
    fit.fit();
    // 桌面端挂载即聚焦可直接打字；移动端不自动聚焦（打开面板不弹输入法），
    // 由 onTouchEnd 的「轻点终端」手势聚焦 xterm 隐藏 textarea 弹出输入法，
    // 键入直接进 PTY——@ 文件引用、/ 命令补全等都由 CLI 自身在终端里渲染。
    // 输入法弹出时 visualViewport 机制会收缩面板高度并 refit，不会盖住终端内容。
    const isDesktopLayout = () => window.matchMedia("(min-width: 768px)").matches;
    if (isDesktopLayout()) term.focus();

    let ws: WebSocket | null = null;
    let retry: number | null = null;
    let scrollFrame: number | null = null;
    let attempts = 0;
    let disposed = false;
    const touchDevice = isTouchDevice();
    const deferredWrites: (string | Uint8Array)[] = [];
    let deferredBytes = 0;
    let touchActive = false;
    let pauseOverflowed = false;
    let applicationScrollActive = false;
    let pasteUploadsInFlight = 0;
    let selectionResumeTimer: number | null = null;
    // 合成滚轮补滚的闸门与节流：CC 开着鼠标跟踪时，合成 WheelEvent 会被 xterm 编码成 SGR
    // 鼠标序列直发 PTY。一次性同步灌几百条会被 CC 当成菜单里的连续选择（实测打出过两次
    // /clear 清空会话），所以总量降到 APPLICATION_SCROLL_STEPS 并按帧摊开，且加冷却。
    let applicationScrollFrame: number | null = null;
    let applicationScrollCooldownUntil = 0;
    // >0 表示正有 term.write 在处理中：其间 xterm 触发的"到底部"是应用写入(codex 原地重绘)
    // 所致，不是用户手势，不得据此重启自动跟随。
    let appWritesInFlight = 0;

    const getNativeTerminalSelection = () => {
      const selection = document.getSelection();
      if (
        !selection?.anchorNode ||
        !selection.focusNode ||
        selection.isCollapsed ||
        !terminalHost.contains(selection.anchorNode) ||
        !terminalHost.contains(selection.focusNode)
      ) {
        return "";
      }
      return selection.toString();
    };
    const hasNativeTerminalSelection = () => !!getNativeTerminalSelection();
    const clearNativeTerminalSelection = () => {
      const selection = document.getSelection();
      selection?.removeAllRanges();
      term.clearSelection();
    };
    // xterm paints its own selection, so the browser's native Copy command sees an empty DOM
    // selection unless we explicitly populate the synchronous clipboard event.
    const getTerminalSelection = () => getNativeTerminalSelection() || term.getSelection();
    const onTerminalCopy = (event: ClipboardEvent) => {
      const selection = getTerminalSelection();
      if (!selection || !event.clipboardData) return;
      event.clipboardData.setData("text/plain", selection);
      event.preventDefault();
    };
    const copyTerminalSelection = () => {
      const selection = getTerminalSelection();
      if (!selection) return;
      const legacyCopy = () => {
        if (!document.execCommand("copy")) toast("复制失败，请检查浏览器剪贴板权限", "error");
      };
      if (navigator.clipboard?.writeText) {
        void navigator.clipboard.writeText(selection).catch(legacyCopy);
      } else {
        legacyCopy();
      }
    };
    terminalHost.addEventListener("copy", onTerminalCopy);
    term.attachCustomKeyEventHandler((event) => {
      if (event.type !== "keydown") return true;
      const isCopy = (event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "c";
      if (!isCopy || (!hasNativeTerminalSelection() && !term.hasSelection())) return true;
      copyTerminalSelection();
      return false;
    });
    const onTerminalPaste = (event: ClipboardEvent) => {
      if (!liveRef.current) return;
      const itemImages = Array.from(event.clipboardData?.items ?? [])
        .filter((item) => item.kind === "file" && item.type.startsWith("image/"))
        .map((item) => item.getAsFile())
        .filter((file): file is File => file !== null);
      const images = itemImages.length
        ? itemImages
        : Array.from(event.clipboardData?.files ?? []).filter((file) =>
            file.type.startsWith("image/"),
          );
      if (images.length === 0) return;

      // The browser owns a remote client's clipboard; the server-side PTY cannot read it.
      // Stop xterm/Codex from handling the image shortcut, upload it, then type server paths.
      event.preventDefault();
      event.stopPropagation();
      pasteUploadsInFlight += 1;
      setPastingImages(true);
      void (async () => {
        const paths: string[] = [];
        for (const image of images) {
          try {
            const uploaded = await api.uploadFile(taskId, image);
            paths.push(uploaded.path);
          } catch (error) {
            toast(
              `粘贴图片上传失败：${error instanceof Error ? error.message : String(error)}`,
              "error",
            );
          }
        }
        if (paths.length > 0) {
          const socket = wsRef.current;
          if (socket?.readyState === WebSocket.OPEN) {
            socket.send(` ${paths.join(" ")} `);
            scrollToLatest();
            toast(`已粘贴 ${paths.length} 张图片`);
          } else {
            toast("图片已上传，但终端连接已断开，请重连后用文件按钮选择", "error");
          }
        }
      })().finally(() => {
        pasteUploadsInFlight = Math.max(0, pasteUploadsInFlight - 1);
        if (!disposed && pasteUploadsInFlight === 0) setPastingImages(false);
      });
    };
    terminalHost.addEventListener("paste", onTerminalPaste, true);

    const setBottomState = (next: boolean) => {
      setAtBottom((prev) => (prev === next ? prev : next));
    };

    const scheduleScrollToBottom = () => {
      if (disposed || scrollFrame != null) return;
      if (applicationScrollActive) {
        setBottomState(false);
        return;
      }
      if (!stickRef.current && userScrolledRef.current) return;
      scrollFrame = window.requestAnimationFrame(() => {
        scrollFrame = null;
        if (disposed || applicationScrollActive || (!stickRef.current && userScrolledRef.current)) {
          if (applicationScrollActive) setBottomState(false);
          return;
        }
        term.scrollToBottom();
        setBottomState(true);
      });
    };

    const writeNow = (
      data: string | Uint8Array,
      onDone?: () => void,
      suppressAutoFollow = false,
    ) => {
      if (disposed) return;
      appWritesInFlight += 1;
      term.write(data, () => {
        appWritesInFlight = Math.max(0, appWritesInFlight - 1);
        if (!suppressAutoFollow) scheduleScrollToBottom();
        onDone?.();
      });
    };

    const flushDeferredWrites = (onFlushed?: () => void) => {
      const batch = deferredWrites.splice(0);
      deferredBytes = 0;
      if (batch.length === 0) {
        onFlushed?.();
        return;
      }
      let remaining = batch.length;
      for (const data of batch) {
        writeNow(
          data,
          () => {
            remaining -= 1;
            if (remaining === 0) {
              scheduleScrollToBottom();
              onFlushed?.();
            }
          },
          true,
        );
      }
    };

    const maybeResumeDeferredWrites = () => {
      if (disposed) return;
      if (touchActive || userScrolledRef.current || hasNativeTerminalSelection()) return;
      pauseOverflowed = false;
      flushDeferredWrites();
    };

    const writeOrDefer = (data: string | Uint8Array) => {
      if (disposed) return;
      const shouldPause =
        touchDevice &&
        !pauseOverflowed &&
        (touchActive || userScrolledRef.current || hasNativeTerminalSelection());
      if (!shouldPause) {
        writeNow(data);
        return;
      }
      const nextDeferredBytes = deferredBytes + terminalDataByteLength(data);
      deferredWrites.push(data);
      deferredBytes = nextDeferredBytes;
      if (nextDeferredBytes >= MAX_DEFERRED_TERMINAL_BYTES) {
        pauseOverflowed = true;
        flushDeferredWrites();
      }
    };

    const stopApplicationScroll = () => {
      if (applicationScrollFrame != null) {
        cancelAnimationFrame(applicationScrollFrame);
        applicationScrollFrame = null;
      }
    };
    const scrollApplicationToLatest = () => {
      if (!applicationScrollActive) return;
      // 先销账：补滚要么这次发出去，要么被闸门挡掉，都不该留到下一次 flush 时补发——
      // 那正是「锁屏几分钟后重连，一次 flush 打出整批滚轮」的成因。
      applicationScrollActive = false;
      const element = term.element;
      if (term.modes.mouseTrackingMode === "none" || !element) return;
      // 断连期间发滚轮没有意义：这一批要么丢在半路，要么在重连回放里错位成菜单操作。
      if (wsRef.current?.readyState !== WebSocket.OPEN) return;
      const now = performance.now();
      if (now < applicationScrollCooldownUntil) return;
      applicationScrollCooldownUntil = now + APPLICATION_SCROLL_COOLDOWN_MS;
      const rect = terminalHost.getBoundingClientRect();
      const init: WheelEventInit = {
        bubbles: true,
        cancelable: true,
        clientX: rect.left + rect.width / 2,
        clientY: rect.top + rect.height / 2,
        deltaMode: WheelEvent.DOM_DELTA_PIXEL,
        deltaY: 100,
        view: window,
      };
      // Claude 的 TUI 自己持有历史深度，前端拿不到精确底部，只能有限批量下滚。按帧摊开
      // 而不是同步灌完：机器速度的连发会被 CC 读成菜单里的连续选择。
      stopApplicationScroll();
      let remaining = APPLICATION_SCROLL_STEPS;
      const step = () => {
        applicationScrollFrame = null;
        if (disposed || !term.element) return;
        if (wsRef.current?.readyState !== WebSocket.OPEN) return;
        for (let i = 0; i < APPLICATION_SCROLL_STEPS_PER_FRAME && remaining > 0; i += 1) {
          remaining -= 1;
          term.element.dispatchEvent(new WheelEvent("wheel", init));
        }
        if (remaining > 0) applicationScrollFrame = requestAnimationFrame(step);
      };
      step();
    };

    const forceFlushDeferredWrites = (onFlushed?: () => void) => {
      scrollApplicationToLatest();
      pauseOverflowed = false;
      flushDeferredWrites(onFlushed);
    };
    flushDeferredWritesRef.current = forceFlushDeferredWrites;

    const connect = () => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      const socket = new WebSocket(`${proto}://${location.host}/ws/session/${taskId}`, wsProtocols());
      ws = socket;
      wsRef.current = socket;
      socket.binaryType = "arraybuffer";
      socket.onmessage = (e) => {
        if (disposed || wsRef.current !== socket) return;
        if (typeof e.data === "string") writeOrDefer(e.data);
        else writeOrDefer(new Uint8Array(e.data));
      };
      socket.onopen = () => {
        if (disposed || wsRef.current !== socket) return;
        attempts = 0;
        setDisconnected(false);
        setConnected(true);
        userScrolledRef.current = false;
        stickRef.current = true;
        touchActive = false;
        pauseOverflowed = false;
        applicationScrollActive = false;
        stopApplicationScroll();
        deferredWrites.splice(0);
        deferredBytes = 0;
        setBottomState(true);
        term.reset(); // 后端每次连接都回放 bounded backlog，重置避免叠加重复
        scheduleScrollToBottom();
        // 长日志只回放末尾的局部 TUI diff；改变一次尺寸再恢复，强制 PTY 补画完整画面。
        // 继续使用旧后端也认识的 resize 控制帧，避免滚动更新期间新前端把 redraw 当输入。
        const temporaryRows = term.rows > 1 ? term.rows - 1 : term.rows + 1;
        socket.send(`\x00resize:${temporaryRows},${term.cols}`);
        socket.send(`\x00resize:${term.rows},${term.cols}`);
      };
      socket.onclose = (ev) => {
        if (ev.code === WS_UNAUTHORIZED) {
          setToken(null);
          return;
        }
        const isCurrentSocket = wsRef.current === socket;
        if (isCurrentSocket) wsRef.current = null;
        if (disposed || !isCurrentSocket) return;
        setConnected(false);
        // 断连即销掉未兑现的补滚，别让它跨越重连补发（锁屏恢复时曾借此打出滚轮风暴）
        applicationScrollActive = false;
        stopApplicationScroll();
        // 任务已结束：后端回放日志后主动关闭，不重连；仍在运行才退避重连
        if (!liveRef.current) return;
        setDisconnected(true);
        attempts += 1;
        const delay = Math.min(1000 * 2 ** attempts, 10000);
        retry = window.setTimeout(connect, delay);
      };
      socket.onerror = () => {
        if (!disposed) socket.close();
      };
    };
    connect();

    term.onData((d) => wsRef.current?.readyState === WebSocket.OPEN && wsRef.current.send(d));
    const scrollDisposable = term.onScroll((viewportY) => {
      if (disposed) return;
      if (applicationScrollActive) {
        setBottomState(false);
        return;
      }
      const bottom = term.buffer.active.baseY - viewportY <= 2;
      if (bottom) {
        // 只有用户本人手势滚到底、或本来就没脱离底部时才恢复自动跟随。
        // Codex 用滚动区+光标定位原地重绘会自发把视图带到底部，不能借此取消用户的
        // 上滑意图，否则输出期间用户会被反复拽回最新（跑完不再重绘才滚得动）。
        // 关键：写入进行中(appWritesInFlight>0)的到底部就是重绘所致，直接排除；时间窗口
        // 分不清"用户滚到底"与"重绘跳到底"（重绘太频繁，几乎总落在手势后的窗口内）。
        const byUserGesture = performance.now() - lastUserScrollAtRef.current < 250;
        if (appWritesInFlight === 0 && (byUserGesture || !userScrolledRef.current)) {
          userScrolledRef.current = false;
          stickRef.current = true;
          maybeResumeDeferredWrites();
        }
      } else if (userScrolledRef.current) {
        stickRef.current = false;
      }
      setBottomState(bottom);
    });
    const markUserScroll = (event?: Event) => {
      if (event && !event.isTrusted) return;
      userScrolledRef.current = true;
      stickRef.current = false;
      lastUserScrollAtRef.current = performance.now();
    };
    const markKeyboardScroll = (e: KeyboardEvent) => {
      if (["End", "Home", "PageDown", "PageUp"].includes(e.key)) markUserScroll();
    };
    const focusTerminal = () => {
      // 桌面端按下即聚焦；移动端 pointerdown 也是滚动手势的起点，不能在这里聚焦
      // （会导致一拖就弹输入法），改由 onTouchEnd 判定「轻点」后聚焦
      if (isDesktopLayout()) term.focus();
    };
    // 移动端触摸滚动：纵向拖拽转成 scrollLines 翻历史；长按选择由系统接管、横向拖动不接管。
    // 不用计时器区分滚动/长按——移动端选择必先长按出选区，届时 hasNativeTerminalSelection()
    // 让位即可；用时间闸会把"按住停顿再上滑"误判成长按，导致上滑翻不了历史。
    // xterm 的文字层浮在可滚 viewport 之上且是兄弟节点，原生触摸滚动够不到 viewport，所以只能
    // 自绘手势；1:1 拖拽无惯性 → 长历史"只能上滑一点点"。故追踪速度，松手后带摩擦衰减续滚(fling)。
    let tStartX = 0;
    let tLastX = 0;
    let tStartY = 0;
    let tLastY = 0;
    let tStartAt = 0;
    let tapFocusUntil = 0;
    let tRemainder = 0;
    let tActive = false;
    let tScrollStarted = false;
    let tLastMoveAt = 0;
    let tVelocity = 0; // 手指纵向速度(px/ms)，平滑估计，用于松手甩滚
    let flingFrame: number | null = null;
    const cellHeight = () => {
      const rows = term.rows || 1;
      const vp = terminalHost.querySelector<HTMLElement>(".xterm-viewport");
      const h = vp?.clientHeight || terminalHost.clientHeight || rows * 17;
      return Math.max(1, h / rows);
    };
    const stopFling = () => {
      if (flingFrame != null) {
        cancelAnimationFrame(flingFrame);
        flingFrame = null;
      }
    };
    const dispatchApplicationWheel = (lines: number): boolean => {
      const applicationMouseActive =
        term.modes.mouseTrackingMode !== "none" && !!term.element;
      if (!applicationMouseActive || lines === 0) return false;
      if (touchActive) {
        touchActive = false;
        flushDeferredWrites();
      }
      applicationScrollActive = true;
      setBottomState(false);
      term.element?.dispatchEvent(
        new WheelEvent("wheel", {
          bubbles: true,
          cancelable: true,
          clientX: tLastX,
          clientY: tLastY,
          deltaMode: WheelEvent.DOM_DELTA_PIXEL,
          deltaY: lines * cellHeight(),
          view: window,
        }),
      );
      return true;
    };
    // 行内亚像素偏移：xterm 只能整行滚(scrollLines)，纯行量化的拖动每步跳一个字符行，
    // 观感明显发涩。把不足一行的余量用 translateY 补到画布上，视觉上做到像素级跟手；
    // 松手停稳/撞边界/滚动交给 TUI 时把偏移吸回行网格。
    const screenEl = terminalHost.querySelector<HTMLElement>(".xterm-screen");
    if (screenEl) screenEl.style.willChange = "transform";
    let subCellOffset = 0;
    let subCellSnapTimer: number | null = null;
    const setSubCellOffset = (px: number) => {
      if (!screenEl || subCellOffset === px) return;
      subCellOffset = px;
      screenEl.style.transform = px ? `translateY(${px}px)` : "";
    };
    const clearSubCellTransition = () => {
      if (subCellSnapTimer != null) {
        window.clearTimeout(subCellSnapTimer);
        subCellSnapTimer = null;
      }
      if (screenEl) screenEl.style.transition = "";
    };
    // 松手吸回行网格：残余偏移 ≤ 半个字符行，用一段短过渡回去，避免肉眼可见的跳变
    const snapSubCellOffset = () => {
      tRemainder = 0;
      if (!screenEl || subCellOffset === 0) return;
      screenEl.style.transition = "transform 90ms ease-out";
      setSubCellOffset(0);
      if (subCellSnapTimer != null) window.clearTimeout(subCellSnapTimer);
      subCellSnapTimer = window.setTimeout(() => {
        subCellSnapTimer = null;
        if (screenEl) screenEl.style.transition = "";
      }, 120);
    };
    // 按手指位移滚动，返回是否真的动了(撞到顶/底则不动，用于停止甩滚)
    const scrollByFingerDelta = (dy: number): boolean => {
      tRemainder += dy / cellHeight();
      const lines = Math.trunc(tRemainder);
      tRemainder -= lines;
      if (lines !== 0 && dispatchApplicationWheel(lines)) {
        // TUI 接管滚动（鼠标跟踪模式），画布归 TUI 重绘，不做亚像素偏移
        tRemainder = 0;
        setSubCellOffset(0);
        return true;
      }
      const buffer = term.buffer.active;
      let moved = true;
      if (lines !== 0) {
        markUserScroll();
        const before = buffer.viewportY;
        // xterm: 负数向上（历史）、正数向下（最新）。上滑时 dy/lines<0，必须原样传入；
        // 取反会让上滑变成向下滚，而终端初始已在底部，于是视觉上完全没有反应。
        term.scrollLines(lines);
        moved = buffer.viewportY !== before;
      }
      // 撞到顶/底后余量清零，不把画布拖出边界露出底色
      if (
        (buffer.viewportY >= buffer.baseY && tRemainder > 0) ||
        (buffer.viewportY <= 0 && tRemainder < 0)
      ) {
        tRemainder = 0;
      }
      // 视口下移 1 行 = 内容上移 1 行，故余量 f 行对应 translateY(-f·行高)
      setSubCellOffset(-tRemainder * cellHeight());
      return lines === 0 ? true : moved;
    };
    const startFling = () => {
      stopFling();
      if (Math.abs(tVelocity) < 0.05) return; // 太慢(<50px/s)不甩，等同点按/慢拖
      let vel = tVelocity; // px/ms
      let last = performance.now();
      const step = () => {
        flingFrame = null;
        if (disposed) return;
        const now = performance.now();
        const dt = Math.min(50, now - last); // 后台切回大跳做钳制
        last = now;
        const moved = scrollByFingerDelta(vel * dt);
        vel *= Math.pow(0.998, dt); // iOS 风格逐毫秒衰减(≈0.968/16ms)，滑得更远更绵
        if (!moved || Math.abs(vel) < 0.01) {
          snapSubCellOffset(); // 到顶/到底或速度衰减尽 → 停，吸回行网格
          return;
        }
        flingFrame = requestAnimationFrame(step);
      };
      flingFrame = requestAnimationFrame(step);
    };
    const onTouchStart = (e: globalThis.TouchEvent) => {
      if (e.touches.length !== 1) return;
      const t = e.touches[0];
      if (!t) return;
      e.stopPropagation();
      touchActive = true;
      pauseOverflowed = false;
      stopFling(); // 手指按下打断上一次甩滚
      tStartX = tLastX = t.clientX;
      tStartY = tLastY = t.clientY;
      tStartAt = performance.now();
      tLastMoveAt = performance.now();
      tVelocity = 0;
      // 打断甩滚接着拖：把画布上尚未吸回的亚像素偏移换算回余量，保证视觉连续不跳
      clearSubCellTransition();
      tRemainder = subCellOffset ? -subCellOffset / cellHeight() : 0;
      tScrollStarted = false;
      tActive = true;
    };
    const onTouchMove = (e: globalThis.TouchEvent) => {
      if (!tActive) return;
      const t = e.touches[0];
      if (!t) return;
      e.stopPropagation();
      const totalDx = t.clientX - tStartX;
      const totalDy = t.clientY - tStartY;
      if (!tScrollStarted) {
        if (Math.abs(totalDy) < 8) return; // 等纵向位移过阈值再接管，短于此留给点按/长按
        if (Math.abs(totalDy) < Math.abs(totalDx)) return; // 横向拖选/手势不接管
        tScrollStarted = true;
      }
      // iOS WebKit 在 touch-action:none 下仍可能先形成文本选区。确认是纵向滚动后清掉
      // 这次临时选区再接管；静止长按和横向拖选会在上面的阈值/方向判断处保留。
      if (hasNativeTerminalSelection()) clearNativeTerminalSelection();
      const dy = t.clientY - tLastY;
      tLastX = t.clientX;
      tLastY = t.clientY;
      const now = performance.now();
      const dt = now - tLastMoveAt;
      tLastMoveAt = now;
      if (dt > 0 && dt < 200) tVelocity = 0.6 * tVelocity + 0.4 * (dy / dt); // 平滑瞬时速度
      e.preventDefault(); // 进入短滑滚动后独占纵向滚动，避免 xterm/页面滚动叠加
      scrollByFingerDelta(dy);
    };
    const onTouchEnd = (e: globalThis.TouchEvent) => {
      const wasActive = tActive;
      if (tActive) e.stopPropagation();
      tActive = false;
      touchActive = false;
      if (tScrollStarted) startFling(); // 松手按末速甩滚，撞顶/底或衰减尽自停
      if (tScrollStarted && flingFrame == null) snapSubCellOffset(); // 慢速松手没起甩滚 → 直接吸回
      // 移动端「轻点终端」＝想输入：聚焦 xterm 隐藏 textarea 弹出输入法，键入直进 PTY，
      // 与桌面端点击终端即可打字一致。纵向拖动(滚历史)、长按选区、pinch 都不算轻点。
      if (
        wasActive &&
        !tScrollStarted &&
        e.type === "touchend" &&
        liveRef.current &&
        !isDesktopLayout() &&
        performance.now() - tStartAt < 350 &&
        !hasNativeTerminalSelection() &&
        !term.hasSelection()
      ) {
        term.focus();
        // 浏览器会对这次轻点补发模拟鼠标事件，落在不可聚焦画布上的 mousedown 默认行为
        // 会把刚聚焦的 textarea 又 blur 掉（表现为输入法一闪就收）。标记一个短窗口，
        // 让链尾的 click（晚于 mousedown）再补一次聚焦兜底。
        tapFocusUntil = performance.now() + 500;
      }
      if (selectionResumeTimer != null) window.clearTimeout(selectionResumeTimer);
      selectionResumeTimer = window.setTimeout(() => {
        selectionResumeTimer = null;
        maybeResumeDeferredWrites();
      }, 150);
    };
    // 轻点聚焦的兜底：touchend 里的 focus 可能被随后的模拟 mousedown 默认行为 blur 掉，
    // 在模拟事件链最后的 click 上再聚焦一次（仍属用户手势，允许弹出输入法）。
    const onTerminalTapFocusClick = () => {
      if (performance.now() > tapFocusUntil) return;
      tapFocusUntil = 0;
      term.focus();
    };
    terminalHost.addEventListener("click", onTerminalTapFocusClick);
    const touchCaptureOptions = { capture: true, passive: false } as AddEventListenerOptions;
    const touchEndCaptureOptions = { capture: true, passive: true } as AddEventListenerOptions;
    terminalHost.addEventListener("pointerdown", focusTerminal);
    // xterm 会先在内部 viewport 处理滚轮/翻页键并同步触发 onScroll。如果等事件冒泡到
    // terminalHost 才标记，onScroll 看不到“用户滚动”，下一帧的新输出仍会把视图拉回底部。
    // 捕获阶段先记录意图，实际离开底部后 onScroll 就会关闭自动跟随。
    const userScrollCaptureOptions = { capture: true, passive: true } as AddEventListenerOptions;
    terminalHost.addEventListener("wheel", markUserScroll, userScrollCaptureOptions);
    terminalHost.addEventListener("touchstart", onTouchStart, touchCaptureOptions);
    terminalHost.addEventListener("touchmove", onTouchMove, touchCaptureOptions);
    terminalHost.addEventListener("touchend", onTouchEnd, touchEndCaptureOptions);
    terminalHost.addEventListener("touchcancel", onTouchEnd, touchEndCaptureOptions);
    terminalHost.addEventListener("keydown", markKeyboardScroll, true);
    document.addEventListener("selectionchange", maybeResumeDeferredWrites);
    // iOS PWA 锁屏/切后台会冻结页面：回来时的那次 flush 不该替几分钟前的手势补滚。
    const onVisibilityChange = () => {
      if (document.visibilityState === "visible") return;
      applicationScrollActive = false;
      stopApplicationScroll();
    };
    document.addEventListener("visibilitychange", onVisibilityChange);

    const onResize = () => {
      const shouldRefocus =
        isDesktopLayout() && !!terminalHost.contains(document.activeElement);
      fit.fit();
      if (shouldRefocus) term.focus();
      scheduleScrollToBottom();
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(`\x00resize:${term.rows},${term.cols}`);
      }
    };
    window.addEventListener("resize", onResize);
    // 容器高度变化（如折叠/展开上方详情面板）时也要 refit
    const ro = new ResizeObserver(onResize);
    ro.observe(elRef.current);
    return () => {
      disposed = true;
      stopFling();
      stopApplicationScroll();
      if (scrollFrame != null) cancelAnimationFrame(scrollFrame);
      if (selectionResumeTimer != null) window.clearTimeout(selectionResumeTimer);
      if (subCellSnapTimer != null) window.clearTimeout(subCellSnapTimer);
      scrollDisposable.dispose();
      ro.disconnect();
      terminalHost.removeEventListener("pointerdown", focusTerminal);
      terminalHost.removeEventListener("click", onTerminalTapFocusClick);
      terminalHost.removeEventListener("copy", onTerminalCopy);
      terminalHost.removeEventListener("paste", onTerminalPaste, true);
      terminalHost.removeEventListener("wheel", markUserScroll, userScrollCaptureOptions);
      terminalHost.removeEventListener("touchstart", onTouchStart, touchCaptureOptions);
      terminalHost.removeEventListener("touchmove", onTouchMove, touchCaptureOptions);
      terminalHost.removeEventListener("touchend", onTouchEnd, touchEndCaptureOptions);
      terminalHost.removeEventListener("touchcancel", onTouchEnd, touchEndCaptureOptions);
      terminalHost.removeEventListener("keydown", markKeyboardScroll, true);
      document.removeEventListener("selectionchange", maybeResumeDeferredWrites);
      document.removeEventListener("visibilitychange", onVisibilityChange);
      window.removeEventListener("resize", onResize);
      if (retry) clearTimeout(retry);
      ws?.close();
      if (wsRef.current === ws) wsRef.current = null;
      setConnected(false);
      if (termRef.current === term) termRef.current = null;
      if (flushDeferredWritesRef.current === forceFlushDeferredWrites) {
        flushDeferredWritesRef.current = null;
      }
      applicationScrollActive = false;
      deferredWrites.splice(0);
      deferredBytes = 0;
      disposeAltBufferWorkaround();
      hardWrappedLinks.dispose();
      term.dispose();
    };
  }, [taskId]);

  return (
    <div className="flex h-full w-full flex-col">
      <div className="relative min-h-0 flex-1">
        {disconnected && (
          <div className="absolute right-2 top-2 z-10 flex items-center gap-1.5 rounded-md bg-rose-500/90 px-2 py-1 text-[11px] text-white shadow">
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-white" />
            连接断开，正在重连…
          </div>
        )}
        {pastingImages && (
          <div className="absolute left-1/2 top-2 z-10 -translate-x-1/2 rounded-md bg-teal-600/95 px-2.5 py-1 text-[11px] font-medium text-white shadow">
            图片上传中…
          </div>
        )}
        {!atBottom && (
          <button
            type="button"
            className="absolute bottom-3 right-4 z-10 rounded-md bg-slate-700/90 px-2 py-1 text-[11px] font-medium text-slate-100 shadow hover:bg-slate-600"
            onClick={scrollToLatest}
            title="跳到最新输出"
          >
            最新 ↓
          </button>
        )}
        <div ref={elRef} className="dh-terminal-selectable h-full w-full overflow-hidden bg-[#101722]" />
      </div>
      {live && (
        <TerminalMobileComposer
          taskId={taskId}
          connected={connected}
          suggestion={suggestion}
          onSend={sendTerminalData}
          onPaste={pasteTerminalData}
          onFocusTerminal={() => termRef.current?.focus()}
          onSmartGenerate={onSmartGenerate}
          smartLoading={smartLoading}
        />
      )}
    </div>
  );
}

function TerminalMobileComposer({
  taskId,
  connected,
  suggestion,
  onSend,
  onPaste,
  onFocusTerminal,
  onSmartGenerate,
  smartLoading,
}: {
  taskId: number;
  connected: boolean;
  suggestion?: ReplySuggestion | null;
  onSend: (data: string) => void;
  onPaste: (data: string) => void;
  onFocusTerminal: () => void;
  onSmartGenerate?: () => void;
  smartLoading?: boolean;
}) {
  const [uploading, setUploading] = useState(false);
  const [dismissedSuggestion, setDismissedSuggestion] = useState("");
  const visibleSuggestion =
    suggestion?.available && suggestion.text.trim() && dismissedSuggestion !== suggestion.text
      ? suggestion
      : null;

  useEffect(() => {
    setDismissedSuggestion("");
  }, [suggestion?.text]);

  const acceptSuggestion = () => {
    if (!suggestion?.text.trim()) return;
    // 粘贴到终端输入行（不自动发送），并聚焦终端方便继续编辑后回车
    onPaste(suggestion.text);
    setDismissedSuggestion(suggestion.text);
    onFocusTerminal();
  };

  const sendKey = (data: string) => {
    if (!connected) return;
    onSend(data);
  };

  const sendCommand = (command: string) => {
    if (!connected) return;
    onSend(`${command}${TERMINAL_SUBMIT_KEY}`);
  };

  const onPickFiles = async (files: File[]) => {
    setUploading(true);
    try {
      // 逐个上传，单个失败不影响其余；成功的路径直接粘贴到终端输入行光标处，
      // 与桌面端粘贴图片的行为一致
      const paths: string[] = [];
      for (const file of files) {
        try {
          const { path } = await api.uploadFile(taskId, file);
          paths.push(path);
        } catch (err) {
          toast(`「${file.name}」上传失败：${err instanceof Error ? err.message : String(err)}`);
        }
      }
      if (paths.length > 0) onPaste(` ${paths.join(" ")} `);
    } finally {
      setUploading(false);
    }
  };

  return (
    <div className="dh-safe-bottom-pad flex shrink-0 flex-col gap-1.5 border-t border-slate-700/50 bg-[#101722] px-2 pt-2 md:hidden">
      {/* 6 列 × 2 行；↑ 在上、← ↓ → 在下同列对齐，组成方向键「倒 T」。
          文本输入直接轻点终端区域弹输入法键入。 */}
      <div className="grid grid-cols-6 gap-1.5">
        <TerminalKeyButton disabled={!connected} onSend={() => sendKey("\x1b")} label="Esc" />
        <TerminalKeyButton disabled={!connected} onSend={() => sendKey("\t")} label="Tab" />
        <TerminalKeyButton disabled={!connected} onSend={() => sendKey("\x03")} label="Ctrl-C" />
        <TerminalKeyButton disabled={!connected} onSend={() => sendKey("\x1b[A")} label="↑" />
        <TerminalKeyButton disabled={!connected} onSend={() => sendKey("\x7f")} label="⌫" />
        <AttachmentPicker
          buttonClassName="w-full whitespace-nowrap rounded-md border border-slate-700 bg-slate-800 px-1 py-1.5 text-[12px] font-medium text-slate-200 hover:bg-slate-700 disabled:opacity-40"
          disabled={!connected || uploading}
          onFiles={onPickFiles}
          title="上传文件（可多选），路径会粘贴到终端输入行"
        >
          {uploading ? "…" : "File"}
        </AttachmentPicker>
        <TerminalKeyButton disabled={!connected} onSend={() => sendCommand("commit push")} label="CP" />
        <TerminalKeyButton disabled={!connected} onSend={() => sendKey(" ")} label="Space" />
        <TerminalKeyButton disabled={!connected} onSend={() => sendKey("\x1b[D")} label="←" />
        <TerminalKeyButton disabled={!connected} onSend={() => sendKey("\x1b[B")} label="↓" />
        <TerminalKeyButton disabled={!connected} onSend={() => sendKey("\x1b[C")} label="→" />
        <TerminalKeyButton disabled={!connected} onSend={() => sendKey(TERMINAL_SUBMIT_KEY)} label="Enter" />
      </div>
      {visibleSuggestion && (
        <div className="rounded-lg border border-teal-500/25 bg-teal-500/10 px-2.5 py-2 text-xs text-teal-50">
          <div className="mb-1 flex items-center gap-2">
            <span className="font-medium text-teal-200">
              {visibleSuggestion.source === "llm" ? "✨ 智能建议" : "建议回复"}
            </span>
            <span className="min-w-0 flex-1 truncate text-[11px] text-teal-100/70" title={visibleSuggestion.reason}>
              {visibleSuggestion.reason}
            </span>
            {onSmartGenerate && (
              <button
                type="button"
                className="rounded px-1.5 py-0.5 text-[11px] font-medium text-teal-100 hover:bg-teal-400/15 disabled:opacity-50"
                onClick={onSmartGenerate}
                disabled={smartLoading}
                title="让 Claude 读取当前任务日志，生成更贴合上下文的回复"
              >
                {smartLoading ? "生成中…" : visibleSuggestion.source === "llm" ? "✨ 重新生成" : "✨ 智能生成"}
              </button>
            )}
            <button
              type="button"
              className="rounded px-1.5 py-0.5 text-[11px] font-medium text-teal-100 hover:bg-teal-400/15"
              onClick={acceptSuggestion}
              title="粘贴到终端输入行，不会自动发送"
            >
              采用建议
            </button>
            <button
              type="button"
              className="rounded px-1.5 py-0.5 text-[11px] text-teal-100/70 hover:bg-teal-400/15 hover:text-teal-50"
              onClick={() => setDismissedSuggestion(visibleSuggestion.text)}
              title="关闭建议"
            >
              ✕
            </button>
          </div>
          <button
            type="button"
            className="block w-full text-left leading-relaxed text-teal-50"
            onClick={acceptSuggestion}
            title="粘贴到终端输入行，不会自动发送"
          >
            {visibleSuggestion.text}
          </button>
        </div>
      )}
    </div>
  );
}

function TerminalKeyButton({
  disabled,
  onSend,
  label,
}: {
  disabled: boolean;
  onSend: () => void;
  label: string;
}) {
  return (
    <button
      type="button"
      className="w-full whitespace-nowrap rounded-md border border-slate-700 bg-slate-900 px-1 py-1.5 text-center text-[12px] font-medium text-slate-200 hover:border-slate-600 hover:bg-slate-800 disabled:opacity-40"
      disabled={disabled}
      // 拦掉默认的焦点转移：正在用输入法直打终端时按方向键/Enter，键盘不收起
      onPointerDown={(e) => e.preventDefault()}
      onClick={onSend}
    >
      {label}
    </button>
  );
}

export function TerminalPanel({
  task,
  switchTasks = [],
  onSwitchTask,
  getProjectName,
  onClose,
  onChanged,
  embedded = false,
}: {
  task: Task;
  switchTasks?: Task[];
  onSwitchTask?: (task: Task) => void;
  getProjectName?: (projectId: number) => string;
  onClose: () => void;
  onChanged: () => void;
  // 嵌入模式：去掉固定抽屉定位与遮罩，由父级网格容器决定尺寸（多终端并排用）
  embedded?: boolean;
}) {
  const [detail, setDetail] = useState<Task>(task);
  const { busy, run } = useSingleFlight();
  // 移动端(小于 lg)默认折叠指令+元信息面板，给终端腾显示空间
  const [metaCollapsed, setMetaCollapsed] = useState(() => embedded || window.innerWidth < 1024);
  // 移动端顶部精简：默认只留 编号/状态 + 详情 + 关闭，其余操作与任务信息收进「详情」
  const [showDetails, setShowDetails] = useState(false);
  const [editing, setEditing] = useState(false);
  const [editTitle, setEditTitle] = useState("");
  const [editPrompt, setEditPrompt] = useState("");
  const [perm, setPerm] = useState<{ tool: string; input: string } | null>(null);
  const [suggestion, setSuggestion] = useState<ReplySuggestion | null>(null);
  const [smartLoading, setSmartLoading] = useState(false);
  const [, bumpAttention] = useReducer((n: number) => n + 1, 0);
  const [historyMode, setHistoryMode] = useState<"history" | "terminal">("history");
  // 抽屉全屏：桌面端把 48% 宽的抽屉铺满整屏，终端由 ResizeObserver 自动重新 fit
  const [fullscreen, setFullscreen] = useState(false);
  const touchStartRef = useRef<{ x: number; y: number } | null>(null);
  const panelTopOffset = useTerminalPanelTopOffset();
  useVisualViewportCssVars();
  usePwaResumeViewportRecovery();
  useLockBodyScrollOnTouch();

  function startEdit() {
    setEditTitle(detail.title || "");
    setEditPrompt(detail.prompt);
    setEditing(true);
  }
  async function saveEdit() {
    await run(async () => {
      await api.updateTask(detail.id, { title: editTitle, prompt: editPrompt });
      setEditing(false);
      await refresh();
      onChanged();
    });
  }

  const refresh = useCallback(async () => {
    try {
      setDetail(await api.getTask(task.id));
    } catch {
      /* ignore */
    }
  }, [task.id]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    setDetail(task);
    setPerm(null);
    setEditing(false);
    setHistoryMode("history");
  }, [task.id]);

  // 运行中：每秒重渲染(时长跳动) + 每3秒拉取(token/状态)
  const [, setTick] = useState(0);
  const active = ["running", "waiting_input"].includes(detail.status);
  const permissionSig = perm ? `${perm.tool}:${perm.input}` : "";
  // 未结束(含 queued/draft)都轮询: 排队任务转为运行时能刷新出终端
  const polling = !["done", "failed", "cancelled", "interrupted"].includes(detail.status);
  useEffect(() => {
    let cancelled = false;
    if (!active) {
      setSuggestion(null);
      return;
    }
    api.replySuggestion(detail.id)
      .then((s) => {
        if (cancelled) return;
        setSuggestion((prev) => {
          if (!s.available) return null;
          // 周期轮询的规则建议不覆盖用户手动点“智能生成”得到的 LLM 建议
          if (prev?.source === "llm" && s.source !== "llm") return prev;
          return s;
        });
      })
      .catch(() => {
        if (!cancelled) setSuggestion(null);
      });
    return () => {
      cancelled = true;
    };
  }, [active, detail.id, detail.status, detail.started_at, permissionSig]);

  // 按需让 Claude 读当前任务日志生成针对性回复（覆盖规则建议）
  const generateSmartSuggestion = useCallback(async () => {
    setSmartLoading(true);
    try {
      const s = await api.smartReplySuggestion(detail.id);
      if (s.available && s.text.trim()) setSuggestion(s);
    } catch {
      /* 生成失败保留现有建议 */
    } finally {
      setSmartLoading(false);
    }
  }, [detail.id]);

  useEffect(() => {
    if (!polling) return;
    const t1 = setInterval(() => setTick((x) => x + 1), 1000);
    const t2 = setInterval(refresh, 3000);
    const t3 = setInterval(() => {
      api.getPermission(detail.id).then((r) => setPerm(r.permission)).catch(() => {});
    }, 1500);
    return () => {
      clearInterval(t1);
      clearInterval(t2);
      clearInterval(t3);
    };
  }, [polling, refresh, detail.id]);

  // 有未决授权时状态徽标显示"待授权"（perm 本地轮询已有，比列表接口更实时）
  const s = STATUS_STYLE[taskStatusStyleKey(detail, !!perm)] ?? STATUS_STYLE.draft;
  const hasStoredHistory = !active && !!detail.session_uid;
  const hasSession = active || !!detail.log_path || hasStoredHistory;
  const isDraft = detail.status === "draft";
  const isChat = detail.render_mode === "chat";
  const switchableTasks = switchTasks.length > 0 ? switchTasks : [detail];
  const currentIdx = switchableTasks.findIndex((t) => t.id === detail.id);
  const canSwitch = currentIdx >= 0 && switchableTasks.length > 1;
  const attentionTasks = switchableTasks.filter(
    (t) =>
      t.id !== detail.id &&
      t.status === "waiting_input" &&
      !seenAttention.has(attentionKey(t)),
  );
  const showAttention = attentionTasks.length > 0;

  /** 关闭/跳转即视为已看过：把当前提醒里的所有轮次记下，不再重复弹。 */
  function dismissAttention() {
    attentionTasks.forEach((t) => seenAttention.add(attentionKey(t)));
    bumpAttention();
  }

  function switchToTask(next: Task) {
    if (next.id === detail.id) return;
    onSwitchTask?.(next);
  }

  function switchBy(delta: number) {
    if (!canSwitch || currentIdx < 0) return;
    const next = switchableTasks[(currentIdx + delta + switchableTasks.length) % switchableTasks.length];
    switchToTask(next);
  }

  // 桌面端快速巡检任务：即使焦点在 xterm 内也拦截组合键，但编辑表单时不抢键盘。
  // 使用上下方向与运行任务列表的纵向顺序对应；首尾循环，适合连续查看多条任务。
  useEffect(() => {
    if (!canSwitch) return;
    const onTaskSwitchShortcut = (event: KeyboardEvent) => {
      if (!event.altKey || !event.shiftKey || event.ctrlKey || event.metaKey) return;
      if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;

      const target = event.target instanceof HTMLElement ? event.target : null;
      const isTerminalTarget = !!target?.closest(".xterm");
      const isEditingTarget = !!target?.closest("input, textarea, select, [contenteditable='true']");
      if (editing || (isEditingTarget && !isTerminalTarget)) return;

      event.preventDefault();
      event.stopPropagation();
      switchBy(event.key === "ArrowUp" ? -1 : 1);
    };
    window.addEventListener("keydown", onTaskSwitchShortcut, true);
    return () => window.removeEventListener("keydown", onTaskSwitchShortcut, true);
  }, [canSwitch, currentIdx, detail.id, editing, switchableTasks]);

  function onTouchStart(e: TouchEvent<HTMLDivElement>) {
    const target = e.target as HTMLElement;
    if (target.closest("button,input,textarea,select,.dh-terminal-selectable")) {
      touchStartRef.current = null;
      return;
    }
    const t = e.touches[0];
    touchStartRef.current = { x: t.clientX, y: t.clientY };
  }

  function onTouchEnd(e: TouchEvent<HTMLDivElement>) {
    const start = touchStartRef.current;
    touchStartRef.current = null;
    if (!start || !canSwitch || editing) return;
    const t = e.changedTouches[0];
    const dx = t.clientX - start.x;
    const dy = t.clientY - start.y;
    if (Math.abs(dx) < 70 || Math.abs(dx) < Math.abs(dy) * 1.5) return;
    switchBy(dx < 0 ? 1 : -1);
  }

  async function downloadLog() {
    const { log, source } = await api.getLog(detail.id, "auto");
    const blob = new Blob([log || "(空)"], { type: "text/plain" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `task-${detail.id}${source === "script" ? ".script" : ""}.log`;
    a.click();
    URL.revokeObjectURL(url);
  }

  async function execute() {
    await run(async () => {
      if (!(await guardQuota(detail.engine))) return;
      await api.startTask(detail.id);
      await refresh();
      onChanged();
    });
  }

  async function doContinue(compact: boolean) {
    await run(async () => {
      if (!(await guardQuota(detail.engine))) return;
      let prompt = "";
      if (!compact) {
        const input = await promptDialog("追加指令（留空=只加载上下文继续）", "", {
          attachToTaskId: detail.id,
        });
        if (input === null) return; // 取消弹窗=不继续；留空=只恢复上下文不发指令
        prompt = input;
      }
      try {
        await api.continueTask(detail.id, { prompt, compact, start: true });
        onChanged();
        onClose();
      } catch (e: any) {
        toast(`失败：${e.message}`, "error");
      }
    });
  }

  async function doRecover() {
    await run(async () => {
      // 会话被 /clear 冲掉时：当前执行器跑的是被清空的新会话，而 DB 钉住的 session_uid
      // 仍是原始会话；停掉执行器后 continue 会 --resume 这个原始 uid，把上下文捞回来。
      if (
        !(await confirmDialog(
          `会话可能被 /clear 冲掉了。将停止当前执行器，并从原始会话 ${detail.session_uid?.slice(0, 8)}… 重新加载上下文继续。`,
          { danger: true },
        ))
      )
        return;
      if (!(await guardQuota(detail.engine))) return;
      try {
        await api.cancelTask(detail.id); // cancel 同步终止执行器并置为 cancelled
        await api.continueTask(detail.id, { prompt: "", compact: false, start: true });
        onChanged();
        onClose();
      } catch (e: any) {
        // 失败也不至于卡死：任务已停为 cancelled，可用「继续会话」按钮再试
        toast(`恢复失败：${e.message}`, "error");
      }
    });
  }

  async function switchEngine(target: Engine) {
    if (target === detail.engine) return;
    await run(async () => {
      if (!(await guardQuota(target))) return;
      if (detail.status === "draft") {
        await api.updateTask(detail.id, { engine: target });
        await refresh();
        onChanged();
        return;
      }
      const activeNow = ["queued", "running", "waiting_input"].includes(detail.status);
      const targetLabel = engineShort(target);
      const message = activeNow
        ? `切换到 ${targetLabel} 接力？当前执行器会停止，原任务会保留。`
        : `创建一个 ${targetLabel} 接力任务？原任务会保留。`;
      if (!(await confirmDialog(message))) return;
      const result = await api.handoffTask(detail.id, target, true);
      const next = await api.getTask(result.id);
      onChanged();
      onSwitchTask?.(next);
    });
  }

  async function doExport() {
    try {
      const bundle = await api.exportSession(detail.id);
      const blob = new Blob([JSON.stringify(bundle, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `session-${detail.engine}-${detail.id}.bosun.json`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e: any) {
      toast(`导出失败：${e.message}（会话可能尚未落盘——等任务结束后再分享）`, "error");
    }
  }

  const canSession = !!detail.session_uid;
  const isFinished = ["done", "failed", "cancelled", "interrupted"].includes(detail.status);
  const canComplete = !isFinished || detail.status === "interrupted";

  function nextTaskAfterComplete(): Task | null {
    const others = switchableTasks.filter((t) => t.id !== detail.id);
    if (others.length === 0 || currentIdx < 0) return null;
    return others[currentIdx] ?? others[0];
  }

  async function doComplete() {
    await run(async () => {
      const nextTask = nextTaskAfterComplete();
      await api.completeTask(detail.id);
      onChanged();
      // 嵌入模式（工作台格子）：完成后直接关闭该格，释放工作台槽位
      if (embedded) {
        onClose();
      } else if (nextTask && onSwitchTask) {
        onSwitchTask(nextTask);
      } else {
        await refresh();
      }
    });
  }

  async function respondPerm(allow: boolean) {
    await run(async () => {
      setPerm(null);
      await api.respondPermission(detail.id, allow).catch(() => {});
    });
  }

  async function doRerun() {
    await run(async () => {
      if (!(await guardQuota(detail.engine))) return;
      await api.rerunTask(detail.id);
      onChanged();
      onClose();
    });
  }

  const panel = (
      <div
        className={
          embedded
            ? "relative box-border flex h-full min-h-0 w-full flex-col overflow-hidden bg-white"
            : fullscreen
              ? "relative box-border flex h-full min-h-0 w-full max-w-full flex-col overflow-hidden bg-white shadow-2xl"
              : "relative box-border flex h-full min-h-0 w-full max-w-full flex-col overflow-hidden bg-white shadow-2xl lg:w-[48%] lg:min-w-[560px]"
        }
        style={embedded ? undefined : terminalPanelBodyStyle()}
        onTouchStart={onTouchStart}
        onTouchEnd={onTouchEnd}
      >
        {showAttention && (
          <div className="absolute right-3 top-16 z-20 w-[min(360px,calc(100%-1.5rem))] rounded-xl border border-amber-200 bg-white p-3 shadow-xl">
            <div className="flex items-center gap-2">
              <span className="h-2 w-2 animate-pulse rounded-full bg-amber-500" />
              <div className="min-w-0 flex-1 text-sm font-semibold text-slate-800">
                其他任务需要处理
              </div>
              <button
                className="rounded-md px-1.5 py-0.5 text-xs text-slate-400 hover:bg-slate-50 hover:text-slate-600"
                onClick={dismissAttention}
                title="关闭提醒"
              >
                ✕
              </button>
            </div>
            <div className="mt-2 space-y-1.5">
              {attentionTasks.slice(0, 3).map((t) => {
                const waitingStyle = STATUS_STYLE[taskStatusStyleKey(t)] ?? STATUS_STYLE.waiting_input;
                return (
                  <button
                    key={t.id}
                    className="flex w-full items-start gap-2 rounded-lg border border-slate-100 px-2.5 py-2 text-left hover:border-amber-200 hover:bg-amber-50/60"
                    onClick={() => {
                      dismissAttention();
                      switchToTask(t);
                    }}
                  >
                    <span className={`mt-1 h-2 w-2 rounded-full ${waitingStyle.dot}`} />
                    <span className="min-w-0 flex-1">
                      <span className="flex items-center gap-1.5 text-xs text-slate-400">
                        <span className="font-mono">#{t.id}</span>
                        <span>{getProjectName?.(t.project_id)}</span>
                        <span className={`font-medium ${waitingStyle.text}`}>
                          {waitingStyle.label}
                        </span>
                      </span>
                      <span className="mt-0.5 block truncate text-sm font-medium text-slate-800">
                        {t.title || taskPromptText(t)}
                      </span>
                      {t.report_summary && (
                        <span
                          className="mt-0.5 line-clamp-2 block text-xs text-slate-500"
                          title={t.report_summary}
                        >
                          {t.report_summary}
                        </span>
                      )}
                    </span>
                    <span className="shrink-0 text-xs font-medium text-teal-700">跳转</span>
                  </button>
                );
              })}
              {attentionTasks.length > 3 && (
                <div className="px-2 text-xs text-slate-400">还有 {attentionTasks.length - 3} 个待处理任务</div>
              )}
            </div>
          </div>
        )}
        {/* 头部 */}
        <div className="flex items-center gap-2 overflow-x-auto border-b border-slate-200 px-4 py-3">
          <span className={`h-2.5 w-2.5 shrink-0 rounded-full ${s.dot} ${s.pulse ? "animate-pulse" : ""}`} />
          <span className="shrink-0 font-mono text-sm text-slate-400">#{detail.id}</span>
          <select
            className="shrink-0 rounded border border-slate-200 bg-slate-100 px-1.5 py-0.5 text-xs font-medium uppercase text-slate-600 disabled:opacity-50"
            value={detail.engine}
            disabled={busy}
            onChange={(e) => void switchEngine(e.target.value as Engine)}
            title={detail.status === "draft" ? "切换任务执行器" : "切换执行器并创建接力任务"}
          >
            <option value="cc">CC</option>
            <option value="codex">Codex</option>
            <option value="omp">omp</option>
          </select>
          <span className={`shrink-0 whitespace-nowrap text-sm font-medium ${s.text}`}>{s.label}</span>
          {canSwitch && (
            <div className="flex shrink-0 items-center rounded-lg border border-slate-200 bg-white text-xs text-slate-500">
              <button
                className="px-2 py-1 hover:bg-slate-50"
                onClick={() => switchBy(-1)}
                title="上一个任务（Alt/Option + Shift + ↑）"
              >
                ‹
              </button>
              <span
                className="border-x border-slate-100 px-2 py-1 tabular-nums"
                title="快捷键：Alt/Option + Shift + ↑ / ↓"
              >
                {currentIdx + 1}/{switchableTasks.length}
              </span>
              <span
                className="hidden px-2 py-1 font-mono text-[10px] text-slate-400 xl:inline"
                title="快捷键：Alt/Option + Shift + ↑ / ↓"
              >
                ⌥/Alt ⇧ ↑↓
              </span>
              <button
                className="px-2 py-1 hover:bg-slate-50"
                onClick={() => switchBy(1)}
                title="下一个任务（Alt/Option + Shift + ↓）"
              >
                ›
              </button>
            </div>
          )}
          <div className="ml-auto flex shrink-0 items-center gap-1.5">
            {/* 移动端：详情 toggle，展开下方操作 + 任务信息；桌面端不显示(操作常驻) */}
            <button
              className="whitespace-nowrap rounded-lg border border-slate-200 px-2.5 py-1 text-xs text-slate-600 hover:bg-slate-50 md:hidden"
              onClick={() =>
                setShowDetails((v) => {
                  if (!v) setMetaCollapsed(false); // 展开时同时展开任务信息
                  return !v;
                })
              }
              title="任务详情与操作"
            >
              详情 {showDetails ? "▴" : "▾"}
            </button>
            {/* 非必要操作：移动端收进「详情」(showDetails)，桌面端常驻 */}
            <div className={`${showDetails ? "flex" : "hidden"} items-center gap-1.5 md:flex`}>
              {canComplete && (
                <button
                  className="whitespace-nowrap rounded-lg border border-slate-200 px-2.5 py-1 text-xs text-sky-600 hover:bg-sky-50 disabled:opacity-50"
                  disabled={busy}
                  onClick={doComplete}
                  title="标记完成并切到下一个任务"
                >
                  ✓<span className="hidden sm:inline"> 完成</span>
                </button>
              )}
              <button
                className="whitespace-nowrap rounded-lg border border-slate-200 px-2.5 py-1 text-xs text-slate-600 hover:bg-slate-50"
                onClick={downloadLog}
                title="下载日志"
              >
                ⬇<span className="hidden sm:inline"> 日志</span>
              </button>
              <button
                className="whitespace-nowrap rounded-lg border border-slate-200 px-2.5 py-1 text-xs text-slate-500 hover:bg-rose-50 hover:text-rose-600 disabled:opacity-50"
                disabled={busy}
                onClick={() =>
                  run(async () => {
                    if (await confirmDialog(`删除任务 #${detail.id}？`, { danger: true })) {
                      await api.deleteTask(detail.id);
                      onChanged();
                      onClose();
                    }
                  })
                }
                title="删除任务"
              >
                🗑<span className="hidden sm:inline"> 删除</span>
              </button>
            </div>
            {/* 全屏：仅抽屉模式且桌面端有意义（移动端抽屉本来就是全宽） */}
            {!embedded && (
              <button
                className="hidden whitespace-nowrap rounded-lg border border-slate-200 px-2.5 py-1 text-xs text-slate-600 hover:bg-slate-50 lg:inline-block"
                onClick={() => setFullscreen((v) => !v)}
                title={fullscreen ? "退出全屏" : "全屏"}
              >
                {fullscreen ? "⤡ 退出全屏" : "⛶ 全屏"}
              </button>
            )}
            {/* 关闭：始终常驻，移动端也能随时退出 */}
            <button
              className="whitespace-nowrap rounded-lg border border-slate-200 px-2.5 py-1 text-xs text-slate-600 hover:bg-slate-50"
              onClick={onClose}
              title="关闭"
            >
              ✕<span className="hidden sm:inline"> 关闭</span>
            </button>
          </div>
        </div>

        {/* 指令 + 元信息（记录）——移动端默认隐藏，点顶部「详情」展开；桌面端常显 */}
        <div className={`${showDetails ? "block" : "hidden"} md:block`}>
        {metaCollapsed && !editing ? (
          <button
            className="flex w-full items-center gap-2 border-b border-slate-200 bg-slate-50/60 px-4 py-2 text-left"
            onClick={() => setMetaCollapsed(false)}
            title="展开任务详情"
          >
            <div className="min-w-0 flex-1 truncate text-sm font-semibold text-slate-800">
              {detail.title || taskPromptText(detail)}
            </div>
            <span className="shrink-0 text-xs text-slate-400">详情 ▾</span>
          </button>
        ) : (
        <div className="border-b border-slate-200 bg-slate-50/60 px-4 py-3">
          {editing ? (
            <div className="mb-2 space-y-2">
              <input
                className="w-full rounded-lg border border-slate-200 bg-white px-2 py-1 text-sm text-slate-800 focus:border-teal-500 focus:outline-none"
                placeholder="简短标题"
                value={editTitle}
                onChange={(e) => setEditTitle(e.target.value)}
              />
              <textarea
                className="h-24 w-full rounded-lg border border-slate-200 bg-white p-2 font-mono text-xs text-slate-800 focus:border-teal-500 focus:outline-none"
                value={editPrompt}
                onChange={(e) => setEditPrompt(e.target.value)}
              />
              <div className="flex justify-end gap-2">
                <button
                  className="rounded-lg border border-slate-200 px-2.5 py-1 text-xs text-slate-600 hover:bg-slate-50"
                  onClick={() => setEditing(false)}
                >
                  取消
                </button>
                <button
                  className="rounded-lg bg-teal-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-teal-700 disabled:opacity-50"
                  disabled={busy}
                  onClick={saveEdit}
                >
                  保存
                </button>
              </div>
            </div>
          ) : (
            <div className="mb-2 flex items-start gap-2">
              <div className="min-w-0">
                <div className="text-sm font-semibold text-slate-800">
                  {detail.title || taskPromptText(detail)}
                </div>
                {detail.title && (
                  <div className="mt-0.5 line-clamp-2 text-xs text-slate-500" title={taskPromptText(detail)}>
                    {taskPromptText(detail)}
                  </div>
                )}
              </div>
              <div className="ml-auto flex shrink-0 items-center gap-1.5">
                <button
                  className="rounded-lg border border-slate-200 px-2 py-0.5 text-xs text-slate-500 hover:bg-slate-50"
                  onClick={startEdit}
                >
                  编辑
                </button>
                <button
                  className="rounded-lg border border-slate-200 px-2 py-0.5 text-xs text-slate-500 hover:bg-slate-50"
                  onClick={() => setMetaCollapsed(true)}
                  title="折叠任务详情"
                >
                  收起 ▴
                </button>
              </div>
            </div>
          )}
          <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-[11px] text-slate-500 sm:grid-cols-4">
            <Meta label="优先级" value={`P${detail.priority}`} />
            <Meta label="类型" value={detail.kind === "repair" ? "修复" : detail.kind} />
            <Meta label="退出码" value={detail.exit_code ?? "—"} />
            <Meta label="时长" value={fmtDur(detail.started_at, detail.ended_at, detail.elapsed_accum)} />
            <Meta label="Tokens" value={detail.tokens != null ? detail.tokens.toLocaleString() : "—"} />
            <Meta label="创建" value={fmtTime(detail.created_at)} />
            <Meta label="开始" value={fmtTime(detail.started_at)} />
            <Meta label="结束" value={fmtTime(detail.ended_at)} span2 />
            <Meta label="会话" value={detail.session_uid ? `${detail.session_uid.slice(0, 8)}…` : "未捕获"} span2 />
          </div>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            {!isFinished && active && canSession && detail.session_cleared && (
              <button
                className="rounded-lg border border-amber-300 px-2.5 py-1 text-xs font-medium text-amber-700 hover:bg-amber-50 disabled:opacity-50"
                disabled={busy}
                onClick={doRecover}
                title="终端里检测到 /clear：停止当前执行器，从原始会话重新加载上下文继续"
              >
                ⟳ 恢复会话
              </button>
            )}
            {!isFinished && active && (
              <span className="text-[11px] text-slate-400">
                {detail.session_cleared
                  ? "检测到会话被 /clear 冲掉——可「恢复会话」从原始上下文继续"
                  : `运行中——请在下方${isChat ? "对话框" : "终端"}里操作/回复；如需分叉先「取消」或等它结束`}
              </span>
            )}
            {isFinished && (
              <button
                className="rounded-lg border border-slate-200 px-2.5 py-1 text-xs text-slate-700 hover:bg-slate-50 disabled:opacity-50"
                disabled={busy}
                onClick={doRerun}
                title="用同样指令全新跑一遍(新会话)"
              >
                ↻ 重跑
              </button>
            )}
            {isFinished && canSession && (
              <>
                <button
                  className="rounded-lg bg-teal-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-teal-700 disabled:opacity-50"
                  disabled={busy}
                  onClick={() => doContinue(false)}
                >
                  ▶ 继续会话
                </button>
                <button
                  className="rounded-lg border border-slate-200 px-2.5 py-1 text-xs text-slate-600 hover:bg-slate-50 disabled:opacity-50"
                  disabled={busy}
                  onClick={() => doContinue(true)}
                  title="恢复会话后自动 /compact 压缩上下文"
                >
                  压缩上下文
                </button>
              </>
            )}
            {canSession && (
              <button
                className="rounded-lg border border-slate-200 px-2.5 py-1 text-xs text-slate-600 hover:bg-slate-50"
                onClick={doExport}
                title="导出完整上下文 bundle 分享给别人"
              >
                分享导出
              </button>
            )}
            {isFinished && !canSession && (
              <span className="text-[10px] text-slate-400">无可恢复的会话，只能「重跑」</span>
            )}
          </div>
        </div>
        )}
        </div>

        {/* 权限请求卡片(SDK 结构化: 点允许/拒绝, 不用去终端敲) */}
        {perm && (
          <div className="border-b border-amber-200 bg-amber-50 px-4 py-3">
            <div className="flex items-center gap-2 text-sm font-medium text-amber-800">
              <span className="h-2 w-2 animate-pulse rounded-full bg-amber-500" />
              需要授权：{perm.tool}
            </div>
            <pre className="mt-1.5 max-h-24 overflow-auto whitespace-pre-wrap rounded bg-white/70 p-2 text-[11px] text-slate-600">
              {perm.input}
            </pre>
            <div className="mt-2 flex justify-end gap-2">
              <button
                className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs text-rose-600 hover:bg-rose-50 disabled:opacity-50"
                disabled={busy}
                onClick={() => respondPerm(false)}
              >
                拒绝
              </button>
              <button
                className="rounded-lg bg-emerald-500 px-3 py-1.5 text-xs font-medium text-white hover:bg-emerald-600 disabled:opacity-50"
                disabled={busy}
                onClick={() => respondPerm(true)}
              >
                允许
              </button>
            </div>
          </div>
        )}

        {/* 终端(pty) / 对话面板(SDK) / 占位 */}
        <div
          className={`min-h-0 flex-1 overflow-hidden bg-[#0b0f17] ${
            isChat
              ? ""
              : active
                ? // 移动端运行中：底部安全区由输入条承担；键盘弹出时会收回留白。
                  // 桌面端(md)无输入条，保留 8px 底距。
                  "p-2 pb-0 md:pb-2"
                : "dh-safe-bottom-pad p-2"
          }`}
        >
          {hasSession ? (
            isChat ? (
              <ChatView
                key={`chat-${detail.id}`}
                taskId={detail.id}
                live={active}
                suggestion={suggestion}
                onSmartGenerate={generateSmartSuggestion}
                smartLoading={smartLoading}
              />
            ) : hasStoredHistory ? (
              <div className="flex h-full min-h-0 flex-col">
                <div className="flex shrink-0 items-center gap-1 border-b border-slate-700 bg-[#101722] px-2 py-1.5">
                  <button
                    type="button"
                    className={`rounded-md px-2.5 py-1 text-xs ${
                      historyMode === "history"
                        ? "bg-teal-600 text-white"
                        : "text-slate-400 hover:bg-slate-800 hover:text-slate-200"
                    }`}
                    onClick={() => setHistoryMode("history")}
                  >
                    对话历史
                  </button>
                  <button
                    type="button"
                    className={`rounded-md px-2.5 py-1 text-xs ${
                      historyMode === "terminal"
                        ? "bg-slate-700 text-white"
                        : "text-slate-400 hover:bg-slate-800 hover:text-slate-200"
                    }`}
                    onClick={() => setHistoryMode("terminal")}
                  >
                    原始终端
                  </button>
                  <span className="ml-1 text-[11px] text-slate-500">历史视图会随窗口宽度自动排版</span>
                </div>
                <div className="min-h-0 flex-1">
                  {historyMode === "history" ? (
                    <SessionHistoryView key={`history-${detail.id}`} taskId={detail.id} />
                  ) : detail.log_path ? (
                    <TerminalView key={`terminal-${detail.id}`} taskId={detail.id} live={false} />
                  ) : (
                    <div className="flex h-full items-center justify-center text-xs text-slate-500">
                      该待执行任务还没有原始终端日志
                    </div>
                  )}
                </div>
              </div>
            ) : (
              <TerminalView
                key={`terminal-${detail.id}`}
                taskId={detail.id}
                live={active}
                suggestion={suggestion}
                onSmartGenerate={generateSmartSuggestion}
                smartLoading={smartLoading}
              />
            )
          ) : (
            <div className="flex h-full flex-col items-center justify-center gap-3 text-slate-400">
              <div className="text-sm">{isDraft ? "任务还是待办，尚未执行" : "排队中，等待空闲并发槽…"}</div>
              {isDraft && (
                <button
                  className="rounded-lg bg-emerald-500 px-4 py-1.5 text-sm font-medium text-white hover:bg-emerald-600 disabled:opacity-50"
                  disabled={busy}
                  onClick={execute}
                >
                  ▶ 立即执行
                </button>
              )}
            </div>
          )}
        </div>
      </div>
  );
  if (embedded) return panel;
  return (
    <div
      data-no-pull-refresh
      // Mobile panels start at top: 0 and must cover the app header; otherwise the
      // terminal title, details toggle, and close button are trapped underneath it.
      className="fixed inset-x-0 z-[60] box-border flex justify-end bg-slate-900/20"
      style={terminalPanelViewportStyle(panelTopOffset)}
    >
      {panel}
    </div>
  );
}

function Meta({ label, value, span2 }: { label: string; value: any; span2?: boolean }) {
  return (
    <div className={span2 ? "col-span-2" : ""}>
      <span className="text-slate-400">{label}：</span>
      <span className="text-slate-600">{value}</span>
    </div>
  );
}
