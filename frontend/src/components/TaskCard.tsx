import { useSortable } from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import { STATUS_STYLE, taskStatusStyleKey } from "../theme";
import type { Task } from "../types";
import { useSingleFlight } from "../useSingleFlight";
import { taskPromptText } from "../taskText";

function fmtDur(start: number | null, end: number | null, accum = 0): string | null {
  const cur = start ? Math.max(0, Math.round((end ?? Date.now() / 1000) - start)) : 0;
  const secs = cur + (accum || 0); // accum: 续聊累计的历次时长
  if (!start && !accum) return null;
  if (secs < 60) return `${secs}s`;
  const m = Math.floor(secs / 60);
  return m < 60 ? `${m}m${secs % 60}s` : `${Math.floor(m / 60)}h${m % 60}m`;
}

function fmtTokens(n: number | null): string | null {
  if (n == null) return null;
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}

export function TaskCard({
  task,
  onOpen,
  onStart,
  onComplete,
  onCancel,
  onDelete,
  onRerun,
  onContinue,
  dragDisabled = false,
}: {
  task: Task;
  onOpen: (t: Task) => void;
  onStart: (t: Task) => void | Promise<void>;
  onComplete: (t: Task) => void | Promise<void>;
  onCancel: (t: Task) => void | Promise<void>;
  onDelete: (t: Task) => void | Promise<void>;
  onRerun: (t: Task) => void | Promise<void>;
  onContinue: (t: Task) => void | Promise<void>;
  /** 嵌套渲染的子任务卡片不在 SortableContext 里注册，必须关掉拖拽 */
  dragDisabled?: boolean;
}) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } =
    useSortable({ id: task.id, disabled: dragDisabled });
  const { busy, run: singleFlight } = useSingleFlight();
  // 动作进行中禁用全部动作按钮，避免快速多点触发重复请求
  const run = (fn: (t: Task) => void | Promise<void>) => async () => {
    await singleFlight(() => fn(task));
  };
  const statusKey = taskStatusStyleKey(task);
  const isPerm = statusKey === "waiting_perm";
  const isChoice = statusKey === "waiting_choice";
  const isReview = statusKey === "waiting_review";
  const s = STATUS_STYLE[statusKey] ?? STATUS_STYLE.draft;
  const isDraft = task.status === "draft";
  const isWaiting = task.status === "waiting_input" && !isPerm && !isChoice && !isReview;
  const active = ["running", "waiting_input", "queued", "rate_limited"].includes(task.status);
  // 受控子任务：标出来源，免得看板上多出一条来路不明的任务
  const isSubtask = task.parent_task_id != null;
  const terminal = ["done", "failed", "cancelled", "interrupted"].includes(task.status);

  return (
    <div
      ref={setNodeRef}
      style={{ transform: CSS.Transform.toString(transform), transition }}
      className={`group w-full rounded-xl border bg-dh-surface p-3 text-sm transition ${
        /* 等待介入：卡面保持中性，只用左缘信号条标色（碳黑仪表） */
        isPerm
          ? "border-dh-bsoft border-l-2 border-l-fuchsia-400"
          : isChoice
          ? "border-dh-bsoft border-l-2 border-l-orange-400"
          : isReview
          ? "border-dh-bsoft border-l-2 border-l-cyan-400"
          : isWaiting
          ? "border-dh-bsoft border-l-2 border-l-amber-400"
          : terminal
            ? "border-dh-bsoft opacity-70"
            : "border-dh-bsoft hover:border-dh-border"
      } ${isDragging ? "opacity-60 ring-2 ring-dh-accent" : ""}`}
    >
      {isSubtask && (
        <div className="mb-1.5 text-[11px] text-slate-500">子任务 · 派生自 #{task.parent_task_id}</div>
      )}
      <div className="flex items-center gap-1.5">
        <span
          {...attributes}
          {...listeners}
          className="cursor-grab select-none text-slate-300 hover:text-dh-muted"
          title="拖拽"
        >
          ⠿
        </span>
        <span className={`h-2 w-2 rounded-full ${s.dot} ${s.pulse ? "animate-pulse" : ""}`} />
        <span className="font-mono text-[11px] text-slate-400">#{task.id}</span>
        <span className="rounded bg-dh-s2 px-1.5 text-[10px] font-medium uppercase text-dh-muted">
          {task.engine}
        </span>
        {task.kind === "repair" && (
          <span className="rounded bg-violet-500/15 px-1.5 text-[10px] text-violet-300">修复</span>
        )}
        <span className="ml-auto rounded-full bg-dh-s2 px-1.5 text-[10px] font-medium text-dh-muted">
          P{task.priority}
        </span>
      </div>

      <div className="mt-2 cursor-pointer" onClick={() => onOpen(task)} title={taskPromptText(task)}>
        <div className="line-clamp-1 text-[13px] font-medium leading-snug text-dh-text hover:text-dh-text">
          {task.title || taskPromptText(task)}
        </div>
        {task.title && (
          <div className="mt-0.5 line-clamp-1 text-[11px] text-slate-400">{taskPromptText(task)}</div>
        )}
      </div>

      <div className="mt-2 flex items-center justify-between">
        <span className={`text-[11px] font-medium ${s.text}`}>
          {isPerm && "🔑 "}
          {s.label}
          {terminal && task.ended_at && (
            <span className="ml-1 font-normal text-slate-400">
              · {new Date(task.ended_at * 1000).toLocaleString("zh", {
                month: "numeric",
                day: "numeric",
                hour: "2-digit",
                minute: "2-digit",
                hour12: false,
              })}
            </span>
          )}
        </span>
        <div className="flex flex-wrap justify-end gap-1 opacity-100 transition md:opacity-0 md:group-hover:opacity-100">
          {isDraft && (
            <button
              className="rounded-md bg-dh-accent px-2 py-0.5 text-[11px] text-dh-accfg hover:bg-dh-acchov disabled:opacity-50"
              onClick={run(onStart)}
              disabled={busy}
              title="排入执行"
            >
              ▶
            </button>
          )}
          <button
            className="rounded-md border border-dh-bsoft px-2 py-0.5 text-[11px] text-dh-tsoft hover:bg-dh-hover"
            onClick={() => onOpen(task)}
          >
            终端
          </button>
          {(!terminal || task.status === "interrupted") && (
            <button
              className="rounded-md border border-dh-bsoft px-2 py-0.5 text-[11px] text-sky-300 hover:bg-sky-500/20 disabled:opacity-50"
              onClick={run(onComplete)}
              disabled={busy}
              title="标记完成"
            >
              ✓
            </button>
          )}
          {terminal && task.session_uid && (
            <button
              className="rounded-md bg-dh-accent px-2 py-0.5 text-[11px] text-dh-accfg hover:bg-dh-acchov disabled:opacity-50"
              onClick={run(onContinue)}
              disabled={busy}
              title="加载旧上下文继续"
            >
              ▶继续
            </button>
          )}
          {terminal && (
            <button
              className="rounded-md border border-dh-bsoft px-2 py-0.5 text-[11px] text-dh-tsoft hover:bg-dh-hover disabled:opacity-50"
              onClick={run(onRerun)}
              disabled={busy}
              title="用同样指令全新跑一遍"
            >
              ↻重跑
            </button>
          )}
          {active && (
            <button
              className="rounded-md border border-dh-bsoft px-2 py-0.5 text-[11px] text-rose-400 hover:bg-rose-500/20 disabled:opacity-50"
              onClick={run(onCancel)}
              disabled={busy}
              title="取消运行"
            >
              ✕
            </button>
          )}
          <button
            className="rounded-md border border-dh-bsoft px-2 py-0.5 text-[11px] text-dh-muted hover:bg-rose-500/20 hover:text-rose-400 disabled:opacity-50"
            onClick={run(onDelete)}
            disabled={busy}
            title="删除任务"
          >
            🗑
          </button>
        </div>
      </div>

      {(fmtDur(task.started_at, task.ended_at, task.elapsed_accum) || task.tokens != null) && (
        <div className="mt-1.5 flex items-center gap-3 border-t border-dh-bsoft pt-1.5 text-[10px] text-slate-400">
          {fmtDur(task.started_at, task.ended_at, task.elapsed_accum) && (
            <span title="处理时长(含续聊累计)">⏱ {fmtDur(task.started_at, task.ended_at, task.elapsed_accum)}</span>
          )}
          {task.tokens != null && <span title="token 用量">◆ {fmtTokens(task.tokens)} tok</span>}
        </div>
      )}
    </div>
  );
}
