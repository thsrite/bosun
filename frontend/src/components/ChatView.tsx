import { memo, useCallback, useEffect, useRef, useState, type TouchEvent } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { authHeaders, WS_UNAUTHORIZED, setToken, wsProtocols } from "../auth";

/** SDK(结构化 cc)会话的对话面板：解析后端 NDJSON 事件流，替代 xterm。
 *  实时流 + 断线重连 + 结束后回放 backlog，逻辑对齐 TerminalView。 */

type ChatEvent =
  | { t: "text"; text: string }
  | { t: "user"; text: string }
  | { t: "tool"; name: string; input: string }
  | { t: "result"; tokens: number; cost: number }
  | { t: "perm"; name: string; input: string }
  | { t: "computer_action"; action: string; detail: string }
  | { t: "screenshot"; url: string; alt: string }
  | { t: "error"; msg: string }
  | { t: "raw"; text: string };

const ANSI = /\x1b\[[0-9;]*[A-Za-z]/g;
const HISTORY_PAGE_EVENTS = 60;
const HISTORY_PAGE_CHARS = 30_000;

function eventTextLength(event: ChatEvent): number {
  switch (event.t) {
    case "text":
    case "raw":
    case "user":
      return event.text.length;
    case "tool":
    case "perm":
      return event.name.length + event.input.length;
    case "computer_action":
      return event.action.length + event.detail.length;
    case "screenshot":
      return event.alt.length;
    case "error":
      return event.msg.length;
    default:
      return 1;
  }
}

/** Find one bounded page before `end`. Keeping this character-bounded matters because
 * a single assistant turn can contain far more Markdown than dozens of small events. */
function historyPageStart(events: ChatEvent[], end: number): number {
  let start = end;
  let chars = 0;
  while (start > 0 && end - start < HISTORY_PAGE_EVENTS) {
    const nextLength = eventTextLength(events[start - 1]);
    if (start < end && chars + nextLength > HISTORY_PAGE_CHARS) break;
    start -= 1;
    chars += nextLength;
  }
  return start;
}

function isTouchDevice(): boolean {
  return (window.matchMedia?.("(pointer: coarse)").matches ?? false) || navigator.maxTouchPoints > 0;
}

function focusTextareaWithoutScroll(input: HTMLTextAreaElement | null) {
  if (!input) return;
  try {
    input.focus({ preventScroll: true });
  } catch {
    input.focus();
  }
  const end = input.value.length;
  try {
    input.setSelectionRange(end, end);
  } catch {
    /* ignore */
  }
}

function parseLines(buf: string): { events: ChatEvent[]; rest: string } {
  const events: ChatEvent[] = [];
  const parts = buf.split("\n");
  const rest = parts.pop() ?? ""; // 末尾可能是半行，留到下次
  for (const line of parts) {
    const s = line.trim();
    if (!s) continue;
    try {
      const obj = JSON.parse(s);
      if (obj && typeof obj.t === "string") {
        events.push(obj as ChatEvent);
        continue;
      }
    } catch {
      /* 非 JSON：降级为纯文本(如老 ANSI 日志 / 会话结束标记) */
    }
    const clean = s.replace(ANSI, "").replace(/\r/g, "").trim();
    if (clean) events.push({ t: "raw", text: clean });
  }
  return { events, rest };
}

export function ChatView({
  taskId,
  live,
  interactive = true,
}: {
  taskId: number;
  live: boolean;
  interactive?: boolean;
}) {
  const [events, setEvents] = useState<ChatEvent[]>([]);
  // Long SDK conversations can contain hundreds of expensive Markdown trees. Keep
  // the full transcript in memory, but mount only a bounded tail until requested.
  const [visibleStart, setVisibleStart] = useState(0);
  const [disconnected, setDisconnected] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const stickRef = useRef(true); // 贴底才自动滚，用户上翻时不打断
  const userScrolledRef = useRef(false);
  // 最近一次用户手势滚动的时间戳，用于区分"用户手动回到底部"与"内容撑高自发到底部"
  const lastUserScrollAtRef = useRef(0);
  const scrollFrameRef = useRef<number | null>(null);
  const [atBottom, setAtBottom] = useState(true);
  const wsRef = useRef<WebSocket | null>(null);
  const liveRef = useRef(live);
  liveRef.current = live;

  const scheduleScrollToBottom = useCallback(() => {
    if (scrollFrameRef.current != null) return;
    if (!stickRef.current && userScrolledRef.current) return;
    scrollFrameRef.current = window.requestAnimationFrame(() => {
      scrollFrameRef.current = null;
      if (!stickRef.current && userScrolledRef.current) return;
      const el = scrollRef.current;
      if (!el) return;
      el.scrollTop = el.scrollHeight;
      setAtBottom(true);
      // Markdown/代码块/图片渲染会在滚动后异步撑高容器，一次 scrollTop 落不到真正底部
      // （打开已有会话时表现为停在顶部）。未贴底且仍在跟随就继续补滚，直到高度稳定。
      if (el.scrollHeight - el.scrollTop - el.clientHeight > 1) scheduleScrollToBottom();
    });
  }, []);

  const scrollToLatest = useCallback(() => {
    userScrolledRef.current = false;
    stickRef.current = true;
    setAtBottom(true);
    if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, []);

  useEffect(() => {
    let ws: WebSocket | null = null;
    let retry: number | null = null;
    let attempts = 0;
    let disposed = false;
    let buf = "";
    let receivedFirstBatch = false;

    const connect = () => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${location.host}/ws/session/${taskId}`, wsProtocols());
      wsRef.current = ws;
      ws.binaryType = "arraybuffer";
      const decoder = new TextDecoder();
      ws.onmessage = (e) => {
        const chunk =
          typeof e.data === "string"
            ? e.data
            : decoder.decode(new Uint8Array(e.data), { stream: true });
        buf += chunk;
        const { events: evs, rest } = parseLines(buf);
        buf = rest;
        if (evs.length) {
          if (!receivedFirstBatch) {
            receivedFirstBatch = true;
            setVisibleStart(historyPageStart(evs, evs.length));
            setEvents(evs);
          } else {
            setEvents((prev) => [...prev, ...evs]);
          }
        }
      };
      ws.onopen = () => {
        attempts = 0;
        buf = "";
        receivedFirstBatch = false;
        setDisconnected(false);
        userScrolledRef.current = false;
        stickRef.current = true;
        setAtBottom(true);
        setVisibleStart(0);
        setEvents([]); // 后端每次连接回放完整 backlog，重置避免重复
        scheduleScrollToBottom();
      };
      ws.onclose = (ev) => {
        if (ev.code === WS_UNAUTHORIZED) {
          setToken(null);
          return;
        }
        if (disposed || !liveRef.current) return;
        setDisconnected(true);
        attempts += 1;
        const delay = Math.min(1000 * 2 ** attempts, 10000);
        retry = window.setTimeout(connect, delay);
      };
      ws.onerror = () => ws?.close();
    };
    connect();

    return () => {
      disposed = true;
      if (retry) clearTimeout(retry);
      ws?.close();
      wsRef.current = null;
    };
  }, [taskId, scheduleScrollToBottom]);

  // 新消息到达且用户贴底 → 滚到底；ResizeObserver 兜住 Markdown/容器高度变化。
  useEffect(() => {
    scheduleScrollToBottom();
  }, [events, scheduleScrollToBottom]);

  useEffect(() => {
    const scrollEl = scrollRef.current;
    const contentEl = contentRef.current;
    if (!scrollEl || !contentEl) return;
    const ro = new ResizeObserver(() => scheduleScrollToBottom());
    ro.observe(scrollEl);
    ro.observe(contentEl);
    return () => ro.disconnect();
  }, [scheduleScrollToBottom]);

  useEffect(() => {
    return () => {
      if (scrollFrameRef.current != null) cancelAnimationFrame(scrollFrameRef.current);
    };
  }, []);

  const onScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const bottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    if (bottom) {
      // 只有用户本人手势滚到底、或本来就没脱离底部时才恢复自动跟随；内容异步撑高自发
      // 到底部不算，否则用户上翻查看历史时会被新消息反复拽回最新。
      const byUserGesture = performance.now() - lastUserScrollAtRef.current < 250;
      if (byUserGesture || !userScrolledRef.current) {
        userScrolledRef.current = false;
        stickRef.current = true;
      }
    } else if (userScrolledRef.current) {
      stickRef.current = false;
    }
    setAtBottom(bottom);
  }, []);

  const markUserScroll = useCallback(() => {
    userScrolledRef.current = true;
    lastUserScrollAtRef.current = performance.now();
  }, []);

  const loadEarlier = useCallback(() => {
    // Loading content above the viewport is a deliberate history action; suppress
    // the ResizeObserver's auto-follow so it cannot pull the user back to latest.
    userScrolledRef.current = true;
    stickRef.current = false;
    setVisibleStart((current) => historyPageStart(events, current));
  }, [events]);

  const visibleEvents = events.slice(visibleStart);

  return (
    <div className="flex h-full flex-col">
      <div className="relative flex-1 overflow-hidden">
        {disconnected && (
          <div className="absolute right-2 top-2 z-10 flex items-center gap-1.5 rounded-md bg-rose-500/90 px-2 py-1 text-[11px] text-white shadow">
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-dh-surface" />
            连接断开，正在重连…
          </div>
        )}
        {!atBottom && (
          <button
            type="button"
            className="absolute bottom-3 right-4 z-10 rounded-md bg-slate-700/90 px-2 py-1 text-[11px] font-medium text-slate-100 shadow hover:bg-slate-600"
            onClick={scrollToLatest}
            title="跳到最新消息"
          >
            最新 ↓
          </button>
        )}
        <div
          ref={scrollRef}
          onScroll={onScroll}
          onWheel={markUserScroll}
          onTouchMove={markUserScroll}
          className="h-full space-y-2 overflow-y-auto px-3 py-3"
        >
          <div ref={contentRef} className="space-y-2">
            {visibleStart > 0 && (
              <div className="sticky top-0 z-[1] flex justify-center py-1">
                <button
                  type="button"
                  className="rounded-md border border-dh-border bg-dh-s2/95 px-3 py-1 text-[11px] font-medium text-slate-300 shadow hover:bg-dh-hover hover:text-white"
                  onClick={loadEarlier}
                >
                  加载更早消息（还有 {visibleStart} 条）
                </button>
              </div>
            )}
            {events.length === 0 && (
              <div className="pt-6 text-center text-xs text-dh-muted">
                {live ? "等待模型输出…" : "(无对话记录)"}
              </div>
            )}
            {visibleEvents.map((ev, i) => (
              <EventBubble key={visibleStart + i} ev={ev} />
            ))}
          </div>
        </div>
      </div>
      {live && interactive && <Composer ws={wsRef} onSend={scrollToLatest} onFocusInput={scrollToLatest} />}
    </div>
  );
}

const EventBubble = memo(function EventBubble({ ev }: { ev: ChatEvent }) {
  switch (ev.t) {
    case "text":
    case "raw":
      return (
        <div className="chat-md rounded-lg bg-dh-s2/40 px-3 py-2">
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            components={{ a: (p) => <a {...p} target="_blank" rel="noreferrer" /> }}
          >
            {ev.text}
          </ReactMarkdown>
        </div>
      );
    case "user":
      return (
        <div className="ml-8 whitespace-pre-wrap break-words rounded-lg bg-dh-accent/15 px-3 py-2 text-[13px] text-dh-text">
          {ev.text}
        </div>
      );
    case "tool":
      return (
        <div className="flex items-start gap-2 rounded-lg border border-cyan-500/25 bg-cyan-500/5 px-3 py-1.5 text-xs">
          <span className="shrink-0 font-medium text-cyan-300">🔧 {ev.name}</span>
          <span className="min-w-0 break-all font-mono text-[11px] text-slate-400">{ev.input}</span>
        </div>
      );
    case "perm":
      return (
        <div className="rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-1.5 text-xs text-amber-300">
          ⚠ 请求授权：<span className="font-medium">{ev.name}</span>
          <span className="ml-1 break-all font-mono text-[11px] text-amber-200/70">{ev.input}</span>
        </div>
      );
    case "computer_action":
      return (
        <div className="flex items-start gap-2 rounded-lg border border-cyan-500/25 bg-cyan-500/5 px-3 py-1.5 text-xs">
          <span className="shrink-0 font-medium text-cyan-300">🖱 {ev.action}</span>
          <span className="min-w-0 break-all font-mono text-[11px] text-slate-400">{ev.detail}</span>
        </div>
      );
    case "screenshot":
      return <ScreenshotBubble url={ev.url} alt={ev.alt} />;
    case "result":
      return (
        <div className="py-1 text-center text-[11px] text-dh-muted">
          — 本轮完成 · {ev.tokens.toLocaleString()} tok
          {ev.cost ? ` · $${ev.cost.toFixed(3)}` : ""} —
        </div>
      );
    case "error":
      return (
        <div className="rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-300">
          [运行错误] {ev.msg}
        </div>
      );
    default:
      return null;
  }
});

const ScreenshotBubble = memo(function ScreenshotBubble({ url, alt }: { url: string; alt: string }) {
  const [src, setSrc] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let disposed = false;
    let objectUrl: string | null = null;
    fetch(url, { headers: authHeaders() })
      .then((response) => {
        if (!response.ok) throw new Error(String(response.status));
        return response.blob();
      })
      .then((blob) => {
        if (disposed) return;
        objectUrl = URL.createObjectURL(blob);
        setSrc(objectUrl);
      })
      .catch(() => { if (!disposed) setFailed(true); });
    return () => {
      disposed = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [url]);

  if (failed) {
    return <div className="rounded-lg border border-rose-500/30 px-3 py-2 text-xs text-rose-300">截图加载失败</div>;
  }
  if (!src) {
    return <div className="rounded-lg border border-dh-bsoft px-3 py-2 text-xs text-dh-muted">正在加载截图…</div>;
  }
  return (
    <a href={src} target="_blank" rel="noreferrer" className="block overflow-hidden rounded-lg border border-dh-bsoft bg-dh-surface">
      <img src={src} alt={alt} className="max-h-[28rem] w-full object-contain" loading="lazy" />
    </a>
  );
});

function Composer({
  ws,
  onSend,
  onFocusInput,
}: {
  ws: React.MutableRefObject<WebSocket | null>;
  onSend: () => void;
  onFocusInput: () => void;
}) {
  const [text, setText] = useState("");
  const inputRef = useRef<HTMLTextAreaElement>(null);

  const send = () => {
    const t = text.trim();
    if (!t || ws.current?.readyState !== WebSocket.OPEN) return;
    ws.current.send(t);
    onSend();
    setText("");
  };
  const onInputTouchStart = (e: TouchEvent<HTMLTextAreaElement>) => {
    if (!isTouchDevice() || document.activeElement === e.currentTarget) return;
    e.preventDefault();
    focusTextareaWithoutScroll(e.currentTarget);
  };
  return (
    <div className="dh-safe-bottom-pad flex flex-col gap-2 border-t border-dh-bsoft bg-[#0b0f17] px-3 pt-2">
      <div className="flex items-end gap-2">
        <textarea
          ref={inputRef}
          className="max-h-28 min-h-[2.35rem] flex-1 resize-none rounded-lg border border-dh-border bg-dh-soft px-2.5 py-1.5 text-[16px] text-slate-100 placeholder-slate-500 focus:border-dh-m2 focus:outline-none"
          rows={1}
          autoCapitalize="none"
          autoCorrect="off"
          spellCheck={false}
          placeholder="输入下一轮指令，Enter 发送 / Shift+Enter 换行"
          value={text}
          onChange={(e) => setText(e.target.value)}
          onFocus={() => window.setTimeout(onFocusInput, 50)}
          onTouchStart={onInputTouchStart}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              send();
            }
          }}
        />
        <button
          className="shrink-0 rounded-lg bg-dh-accent px-3 py-1.5 text-xs font-medium text-dh-accfg hover:bg-dh-acchov disabled:opacity-40"
          onClick={send}
          disabled={!text.trim()}
        >
          发送
        </button>
      </div>
    </div>
  );
}
