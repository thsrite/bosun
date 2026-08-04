import { memo, useCallback, useEffect, useRef, useState, type TouchEvent } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { WS_UNAUTHORIZED, setToken, wsProtocols } from "../auth";
import type { ReplySuggestion } from "../types";

/** SDK(结构化 cc)会话的对话面板：解析后端 NDJSON 事件流，替代 xterm。
 *  实时流 + 断线重连 + 结束后回放 backlog，逻辑对齐 TerminalView。 */

type ChatEvent =
  | { t: "text"; text: string }
  | { t: "user"; text: string }
  | { t: "tool"; name: string; input: string }
  | { t: "result"; tokens: number; cost: number }
  | { t: "perm"; name: string; input: string }
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
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-white" />
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
                  className="rounded-md border border-slate-600 bg-slate-800/95 px-3 py-1 text-[11px] font-medium text-slate-300 shadow hover:bg-slate-700 hover:text-white"
                  onClick={loadEarlier}
                >
                  加载更早消息（还有 {visibleStart} 条）
                </button>
              </div>
            )}
            {events.length === 0 && (
              <div className="pt-6 text-center text-xs text-slate-500">
                {live ? "等待模型输出…" : "(无对话记录)"}
              </div>
            )}
            {visibleEvents.map((ev, i) => (
              <EventBubble key={visibleStart + i} ev={ev} />
            ))}
          </div>
        </div>
      </div>
      {live && (
        <Composer
          ws={wsRef}
          suggestion={suggestion}
          onSend={scrollToLatest}
          onFocusInput={scrollToLatest}
          onSmartGenerate={onSmartGenerate}
          smartLoading={smartLoading}
        />
      )}
    </div>
  );
}

const EventBubble = memo(function EventBubble({ ev }: { ev: ChatEvent }) {
  switch (ev.t) {
    case "text":
    case "raw":
      return (
        <div className="chat-md rounded-lg bg-slate-800/40 px-3 py-2">
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
        <div className="ml-8 whitespace-pre-wrap break-words rounded-lg bg-teal-600/25 px-3 py-2 text-[13px] text-teal-50">
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
    case "result":
      return (
        <div className="py-1 text-center text-[11px] text-slate-500">
          — 本轮完成 · {ev.tokens.toLocaleString()} tok
          {ev.cost ? ` · $${ev.cost.toFixed(3)}` : ""} —
        </div>
      );
    case "error":
      return (
        <div className="rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-300">
          [SDK 错误] {ev.msg}
        </div>
      );
    default:
      return null;
  }
});

function Composer({
  ws,
  suggestion,
  onSend,
  onFocusInput,
  onSmartGenerate,
  smartLoading,
}: {
  ws: React.MutableRefObject<WebSocket | null>;
  suggestion?: ReplySuggestion | null;
  onSend: () => void;
  onFocusInput: () => void;
  onSmartGenerate?: () => void;
  smartLoading?: boolean;
}) {
  const [text, setText] = useState("");
  const [dismissedSuggestion, setDismissedSuggestion] = useState("");
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const canShowSuggestion =
    !!suggestion?.available &&
    !!suggestion.text.trim() &&
    !text.trim() &&
    dismissedSuggestion !== suggestion.text;
  const visibleSuggestion = canShowSuggestion ? suggestion : null;

  useEffect(() => {
    setDismissedSuggestion("");
  }, [suggestion?.text]);

  const acceptSuggestion = () => {
    if (!suggestion?.text.trim()) return;
    setText(suggestion.text);
    setDismissedSuggestion(suggestion.text);
    window.setTimeout(() => focusTextareaWithoutScroll(inputRef.current), 0);
  };

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
    <div className="dh-safe-bottom-pad flex flex-col gap-2 border-t border-slate-700/50 bg-[#0b0f17] px-3 pt-2">
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
              title="填入输入框，不会自动发送"
            >
              Tab 采用
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
            title="填入输入框，不会自动发送"
          >
            {visibleSuggestion.text}
          </button>
        </div>
      )}
      <div className="flex items-end gap-2">
        <textarea
          ref={inputRef}
          className="max-h-28 min-h-[2.35rem] flex-1 resize-none rounded-lg border border-slate-700 bg-slate-900 px-2.5 py-1.5 text-[16px] text-slate-100 placeholder-slate-500 focus:border-teal-500 focus:outline-none"
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
            if (e.key === "Tab" && canShowSuggestion) {
              e.preventDefault();
              acceptSuggestion();
              return;
            }
            if (e.key === "Escape" && visibleSuggestion) {
              e.preventDefault();
              setDismissedSuggestion(visibleSuggestion.text);
              return;
            }
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              send();
            }
          }}
        />
        <button
          className="shrink-0 rounded-lg bg-teal-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-teal-700 disabled:opacity-40"
          onClick={send}
          disabled={!text.trim()}
        >
          发送
        </button>
      </div>
    </div>
  );
}
