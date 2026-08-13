import { api } from "../api";
import { engineName } from "../engines";
import { confirmDialog, toast } from "../overlay";
import type { OrchestrationRun } from "../types";
import { useSingleFlight } from "../useSingleFlight";

const ACTIVE = new Set(["queued", "running", "waiting_input"]);

function snapshotName(run: OrchestrationRun): string {
  try {
    return (JSON.parse(run.definition_snapshot) as { name?: string }).name || "任务编排";
  } catch {
    return "任务编排";
  }
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
              {current && <span className="rounded bg-dh-s2 px-2 py-0.5 text-[11px] text-dh-tsoft">当前角色：{current.name} · {engineName(current.engine)}</span>}
              <span className="ml-auto text-xs text-dh-muted">{item.status}</span>
              {item.status === "draft" && <button type="button" disabled={busy} onClick={(event) => { event.preventDefault(); void start(item); }} className="rounded-md bg-dh-accent px-2.5 py-1 text-xs text-dh-accfg disabled:opacity-50">执行</button>}
              {ACTIVE.has(item.status) && <button type="button" disabled={busy} onClick={(event) => { event.preventDefault(); void cancel(item); }} className="rounded-md border border-rose-500/30 px-2.5 py-1 text-xs text-rose-400 disabled:opacity-50">取消编排</button>}
            </summary>
            <div className="mt-3 space-y-2 border-t border-dh-bsoft pt-3">
              {item.steps.map((step) => (
                <div key={step.id} className="rounded-lg bg-dh-soft px-3 py-2 text-xs">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-mono text-slate-400">{step.position}.</span>
                    <span className="font-medium text-dh-tsoft">{step.name}</span>
                    <span className="rounded bg-dh-s2 px-1.5 py-0.5 text-[10px] uppercase text-dh-muted">{step.engine}</span>
                    {step.model && <span className="rounded bg-dh-s2 px-1.5 py-0.5 text-[10px] text-dh-muted">{step.model}</span>}
                    {step.reasoning_effort && <span className="rounded bg-dh-s2 px-1.5 py-0.5 text-[10px] text-dh-muted">{step.reasoning_effort}</span>}
                    <span className="ml-auto text-slate-400">{step.task_status || step.status}</span>
                  </div>
                  {step.summary && <div className="mt-1 text-slate-400">{step.summary}</div>}
                  {step.output_artifact && <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap rounded bg-dh-surface p-2 text-[11px] text-dh-tsoft">{step.output_artifact}</pre>}
                </div>
              ))}
            </div>
          </details>
        );
      })}
    </section>
  );
}
