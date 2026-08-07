import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api } from "../api";
import type { SessionHistoryMessage } from "../types";

function contentText(content: unknown): string {
  if (typeof content === "string") return content.trim();
  if (!Array.isArray(content)) return "";
  return content
    .flatMap((item) => {
      if (typeof item === "string") return [item];
      if (!item || typeof item !== "object") return [];
      const block = item as Record<string, unknown>;
      if (![undefined, "text", "input_text", "output_text"].includes(block.type as string | undefined)) return [];
      const text = block.text ?? block.content;
      return typeof text === "string" ? [text] : [];
    })
    .join("\n\n")
    .trim();
}

function parseExportedHistory(bundle: { engine?: string; jsonl?: string }) {
  const messages: SessionHistoryMessage[] = [];
  const append = (role: unknown, text: unknown, timestamp: unknown) => {
    if ((role !== "user" && role !== "assistant") || typeof text !== "string") return;
    const clean = text.trim();
    if (!clean || clean.startsWith("<environment_context>")) return;
    const last = messages[messages.length - 1];
    if (last?.role === role && last.text === clean) return;
    messages.push({ role, text: clean, timestamp: typeof timestamp === "string" ? timestamp : null });
  };
  for (const line of (bundle.jsonl || "").split("\n")) {
    let item: Record<string, unknown>;
    try {
      item = JSON.parse(line) as Record<string, unknown>;
    } catch {
      continue;
    }
    const timestamp = item.timestamp;
    if (bundle.engine === "cc") {
      if (item.type !== "user" && item.type !== "assistant") continue;
      const message = item.message as Record<string, unknown> | undefined;
      if (message) append(message.role ?? item.type, contentText(message.content), timestamp);
      continue;
    }
    if (bundle.engine === "omp") {
      // omp: {"type":"message","message":{"role":...,"content":[...]}}
      if (item.type !== "message") continue;
      const message = item.message as Record<string, unknown> | undefined;
      if (message) append(message.role, contentText(message.content), timestamp);
      continue;
    }
    if (bundle.engine === "kimi") {
      // kimi wire.jsonl：用户回合在 turn.prompt(input=[{type:"text",...}])，
      // 助手正文在 context.append_loop_event 的 content.part 事件里。
      if (item.type === "turn.prompt") {
        append("user", contentText(item.input), item.time);
      } else if (item.type === "context.append_loop_event") {
        const event = item.event as Record<string, unknown> | undefined;
        if (event?.type === "content.part") {
          const part = event.part as Record<string, unknown> | undefined;
          if (part?.type === "text") append("assistant", part.text, item.time);
        }
      }
      continue;
    }
    const payload = item.payload as Record<string, unknown> | undefined;
    if (!payload) continue;
    if (item.type === "event_msg" && (payload.type === "user_message" || payload.type === "agent_message")) {
      append(payload.type === "user_message" ? "user" : "assistant", payload.message, timestamp);
    } else if (item.type === "response_item" && payload.type === "message") {
      append(payload.role, contentText(payload.content), timestamp);
    }
  }
  const truncated = messages.length > 300;
  return { messages: truncated ? messages.slice(-300) : messages, truncated };
}

export function SessionHistoryView({ taskId }: { taskId: number }) {
  const [messages, setMessages] = useState<SessionHistoryMessage[]>([]);
  const [truncated, setTruncated] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError("");
    api.getHistory(taskId)
      // 兼容尚未安全重启的旧后端：它已有 export 接口，但还没有 history 接口。
      .catch(() => api.exportSession(taskId).then(parseExportedHistory))
      .then((result) => {
        if (cancelled) return;
        setMessages(result.messages);
        setTruncated(result.truncated);
      })
      .catch((reason: Error) => {
        if (!cancelled) setError(reason.message || "历史读取失败");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [taskId]);

  return (
    <div className="h-full overflow-y-auto bg-[#131316] px-3 py-3 sm:px-5">
      {truncated && (
        <div className="mb-3 rounded-lg border border-amber-500/25 bg-amber-500/10 px-3 py-2 text-xs text-amber-200">
          会话较长，当前展示最近的历史消息；完整记录仍可通过“分享导出”获取。
        </div>
      )}
      {loading && <div className="pt-8 text-center text-xs text-dh-muted">正在读取会话历史…</div>}
      {!loading && error && (
        <div className="pt-8 text-center text-xs text-rose-300">历史读取失败：{error}</div>
      )}
      {!loading && !error && messages.length === 0 && (
        <div className="pt-8 text-center text-xs text-dh-muted">没有可解析的会话历史，可切换到原始终端查看。</div>
      )}
      <div className="mx-auto max-w-5xl space-y-3">
        {messages.map((message, index) =>
          message.role === "user" ? (
            <div
              key={`${message.timestamp ?? "user"}-${index}`}
              className="ml-auto max-w-[92%] whitespace-pre-wrap break-words rounded-xl bg-dh-accent/15 px-3 py-2 text-[13px] leading-relaxed text-dh-text sm:max-w-[80%]"
            >
              {message.text}
            </div>
          ) : (
            <div
              key={`${message.timestamp ?? "assistant"}-${index}`}
              className="chat-md break-words rounded-xl border border-dh-bsoft bg-dh-s2/40 px-3 py-2 text-[13px] leading-relaxed text-slate-200"
            >
              <ReactMarkdown
                remarkPlugins={[remarkGfm]}
                components={{ a: (props) => <a {...props} target="_blank" rel="noreferrer" /> }}
              >
                {message.text}
              </ReactMarkdown>
            </div>
          ),
        )}
      </div>
    </div>
  );
}
