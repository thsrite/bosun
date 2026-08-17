import { useState } from "react";

import { api } from "../api";
import { engineName } from "../engines";
import { confirmDialog, toast } from "../overlay";
import type { OrchestrationMessage, OrchestrationRun, OrchestrationStepRun } from "../types";
import { useSingleFlight } from "../useSingleFlight";

const ACTIVE = new Set(["queued", "running", "waiting_input"]);

const MESSAGE_KIND_LABEL: Record<OrchestrationMessage["kind"], string> = {
  handoff: "交棒",
  rework: "打回",
  ask: "提问",
  answer: "回复",
  system: "系统",
};

/** 角色此刻的班组身份：持棒干活 / 在线待命 / 掉线 / 已收工 */
function roleState(step: OrchestrationStepRun, run: OrchestrationRun) {
  if (step.status === "offline") return { label: "掉线", tone: "text-rose-400" };
  if (step.status === "cancelled") return { label: "已取消", tone: "text-slate-500" };
  if (step.status === "done") return { label: "已交付", tone: "text-sky-400" };
  if (step.status === "failed") return { label: "失败", tone: "text-rose-400" };
  if (step.position === run.current_position) return { label: "持棒中", tone: "text-emerald-400" };
  return { label: "待命", tone: "text-amber-400" };
}

function snapshotName(run: OrchestrationRun): string {
  try {
    return (JSON.parse(run.definition_snapshot) as { name?: string }).name || "任务编排";
  } catch {
    return "任务编排";
  }
}

function MessageTimeline({ runId }: { runId: number }) {
  const [messages, setMessages] = useState<OrchestrationMessage[] | null>(null);
  const [loading, setLoading] = useState(false);

  async function load() {
    setLoading(true);
    try {
      setMessages(await api.orchestrationRuns.messages(runId));
    } catch (error) {
      toast(`读取班组消息失败：${error instanceof Error ? error.message : String(error)}`, "error");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="rounded-lg border border-dh-bsoft bg-dh-soft px-3 py-2 text-xs">
      <div className="flex items-center gap-2">
        <span className="font-medium text-dh-tsoft">班组消息</span>
        <button
          type="button"
          disabled={loading}
          onClick={() => void load()}
          className="rounded border border-dh-bsoft px-2 py-0.5 text-[11px] text-dh-muted disabled:opacity-50"
        >
          {messages === null ? "查看" : "刷新"}
        </button>
        {messages !== null && <span className="text-dh-muted">共 {messages.length} 条</span>}
      </div>
      {messages !== null && messages.length === 0 && (
        <div className="mt-2 text-dh-muted">还没有角色之间的消息。</div>
      )}
      {messages !== null && messages.length > 0 && (
        <ol className="mt-2 space-y-1">
          {messages.map((message) => (
            <li key={message.id} className="flex gap-2 rounded bg-dh-surface px-2 py-1">
              <span className="shrink-0 font-mono text-[10px] text-slate-400">
                {message.from_position ?? "系统"} → {message.to_position}
              </span>
              <span className="shrink-0 rounded bg-dh-s2 px-1.5 text-[10px] text-dh-muted">
                {MESSAGE_KIND_LABEL[message.kind]}
              </span>
              <span className="min-w-0 flex-1 whitespace-pre-wrap break-words text-dh-tsoft">{message.body}</span>
              {message.delivered_at === null && (
                <span className="shrink-0 text-[10px] text-amber-400">待投递</span>
              )}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

export function OrchestrationRunList({ runs, onChanged }: { runs: OrchestrationRun[]; onChanged: () => void }) {
  const { busy, run: singleFlight } = useSingleFlight();
  if (runs.length === 0) return null;

  async function start(item: OrchestrationRun) {
    await singleFlight(async () => {
      try {
        await api.orchestrationRuns.start(item.id);
        onChanged();
      } catch (error) {
        toast(`启动编排失败：${error instanceof Error ? error.message : String(error)}`, "error");
      }
    });
  }

  async function resume(item: OrchestrationRun) {
    await singleFlight(async () => {
      try {
        await api.orchestrationRuns.resume(item.id);
        onChanged();
      } catch (error) {
        toast(`恢复班组失败：${error instanceof Error ? error.message : String(error)}`, "error");
      }
    });
  }

  async function cancel(item: OrchestrationRun) {
    if (!await confirmDialog(`取消编排 #${item.id}「${item.title || snapshotName(item)}」？`, { danger: true })) return;
    await singleFlight(async () => {
      await api.orchestrationRuns.cancel(item.id);
      onChanged();
    });
  }

  return (
    <section className="space-y-2">
      <div className="text-xs font-semibold uppercase tracking-wider text-dh-muted">编排运行</div>
      {runs.map((item) => {
        const current = item.steps.find((step) => step.position === item.current_position);
        const offline = item.steps.filter((step) => step.status === "offline").length;
        const canResume = offline > 0 || (
          item.status === "waiting_input" && current?.result !== "needs_input"
        );
        const total = (() => {
          try { return (JSON.parse(item.definition_snapshot) as { steps?: unknown[] }).steps?.length ?? item.steps.length; }
          catch { return item.steps.length; }
        })();
        return (
          <details key={item.id} className="rounded-xl border border-dh-bsoft bg-dh-surface p-3" open={ACTIVE.has(item.status)}>
            <summary className="flex cursor-pointer list-none flex-wrap items-center gap-2">
              <span className={`h-2 w-2 rounded-full ${item.status === "running" ? "animate-pulse bg-emerald-400" : item.status === "waiting_input" ? "bg-amber-400" : item.status === "done" ? "bg-sky-400" : "bg-slate-500"}`} />
              <span className="font-mono text-[11px] text-slate-400">编排 #{item.id}</span>
              <span className="font-medium text-dh-text">{item.title || snapshotName(item)}</span>
              <span className="text-xs text-slate-400">{snapshotName(item)} · {item.current_position ?? 0}/{total}</span>
              {current && <span className="rounded bg-dh-s2 px-2 py-0.5 text-[11px] text-dh-tsoft">持棒：{current.name} · {engineName(current.engine)}</span>}
              {item.rework_total > 0 && <span className="rounded bg-amber-500/15 px-2 py-0.5 text-[11px] text-amber-400">返工 {item.rework_total} 次</span>}
              {offline > 0 && <span className="rounded bg-rose-500/15 px-2 py-0.5 text-[11px] text-rose-400">{offline} 位掉线</span>}
              <span className="ml-auto text-xs text-dh-muted">{item.status}</span>
              {item.status === "draft" && <button type="button" disabled={busy} onClick={(event) => { event.preventDefault(); void start(item); }} className="rounded-md bg-dh-accent px-2.5 py-1 text-xs text-dh-accfg disabled:opacity-50">执行</button>}
              {ACTIVE.has(item.status) && canResume && <button type="button" disabled={busy} onClick={(event) => { event.preventDefault(); void resume(item); }} className="rounded-md border border-dh-bsoft px-2.5 py-1 text-xs text-dh-tsoft disabled:opacity-50">恢复班组</button>}
              {ACTIVE.has(item.status) && <button type="button" disabled={busy} onClick={(event) => { event.preventDefault(); void cancel(item); }} className="rounded-md border border-rose-500/30 px-2.5 py-1 text-xs text-rose-400 disabled:opacity-50">取消编排</button>}
            </summary>
            <div className="mt-3 space-y-2 border-t border-dh-bsoft pt-3">
              {item.steps.map((step) => {
                const state = roleState(step, item);
                return (
                  <div key={step.id} className="rounded-lg bg-dh-soft px-3 py-2 text-xs">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-mono text-slate-400">{step.position}.</span>
                      <span className="font-medium text-dh-tsoft">{step.name}</span>
                      {step.role_kind === "report" && <span className="rounded bg-sky-500/15 px-1.5 py-0.5 text-[10px] text-sky-400">收口汇报</span>}
                      <span className="rounded bg-dh-s2 px-1.5 py-0.5 text-[10px] uppercase text-dh-muted">{step.engine}</span>
                      {step.model && <span className="rounded bg-dh-s2 px-1.5 py-0.5 text-[10px] text-dh-muted">{step.model}</span>}
                      {step.reasoning_effort && <span className="rounded bg-dh-s2 px-1.5 py-0.5 text-[10px] text-dh-muted">{step.reasoning_effort}</span>}
                      {step.rework_count > 0 && <span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-[10px] text-amber-400">第 {step.attempt} 版 · 被打回 {step.rework_count} 次</span>}
                      <span className={`ml-auto ${state.tone}`}>{state.label}</span>
                    </div>
                    {step.summary && <div className="mt-1 text-slate-400">{step.summary}</div>}
                    {step.output_artifact && <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap rounded bg-dh-surface p-2 text-[11px] text-dh-tsoft">{step.output_artifact}</pre>}
                  </div>
                );
              })}
              <MessageTimeline runId={item.id} />
            </div>
          </details>
        );
      })}
    </section>
  );
}
