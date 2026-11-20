import { useEffect, useState } from "react";

import { api } from "../api";
import { engineName } from "../engines";
import { confirmDialog, toast } from "../overlay";
import { OrchestrationTrace, statusText } from "./OrchestrationTrace";
import type { OrchestrationRun, Task } from "../types";
import { useSingleFlight } from "../useSingleFlight";

const ACTIVE = new Set(["queued", "running", "waiting_input"]);

function snapshotName(run: OrchestrationRun): string {
  try {
    return (JSON.parse(run.definition_snapshot) as { name?: string }).name || "任务编排";
  } catch {
    return "任务编排";
  }
}

export function OrchestrationRunList({ runs, onChanged, onOpenTask }: { runs: OrchestrationRun[]; onChanged: () => void; onOpenTask: (task: Task) => void }) {
  const { busy, run: singleFlight } = useSingleFlight();
  const [expanded, setExpanded] = useState<Record<number, boolean>>({});
  const linked = Number(new URLSearchParams(location.search).get("run"));
  const hasLinkedRun = runs.some(item => item.id === linked);
  useEffect(() => {
    if (hasLinkedRun) document.getElementById("run-" + linked)?.scrollIntoView?.({ block: "start" });
  }, [linked, hasLinkedRun]);
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
        const open = expanded[item.id] ?? (linked ? linked === item.id : ACTIVE.has(item.status));
        return (
          <details key={item.id} id={"run-" + item.id} className="scroll-mt-20 rounded-xl border border-dh-bsoft bg-dh-surface p-3" open={open} onToggle={e => { const value = e.currentTarget.open; if (value !== open) setExpanded(prev => ({ ...prev, [item.id]: value })); }}>
            <summary className="flex cursor-pointer list-none flex-wrap items-center gap-2">
              <span className={`h-2 w-2 rounded-full ${item.status === "running" ? "animate-pulse bg-emerald-400" : item.status === "waiting_input" ? "bg-amber-400" : item.status === "done" ? "bg-sky-400" : "bg-slate-500"}`} />
              <span className="font-mono text-[11px] text-slate-400">编排 #{item.id}</span>
              <span className="font-medium text-dh-text">{item.title || snapshotName(item)}</span>
              <span className="text-xs text-slate-400">{snapshotName(item)} · {item.current_position ?? 0}/{total}</span>
              {current && ACTIVE.has(item.status) && <span className="rounded bg-dh-s2 px-2 py-0.5 text-[11px] text-dh-tsoft">持棒：{current.name} · {engineName(current.engine)}</span>}
              {item.rework_total > 0 && <span className="rounded bg-amber-500/15 px-2 py-0.5 text-[11px] text-amber-400">返工 {item.rework_total} 次</span>}
              {offline > 0 && <span className="rounded bg-rose-500/15 px-2 py-0.5 text-[11px] text-rose-400">{offline} 位掉线</span>}
              <span className="ml-auto text-xs text-dh-muted">{statusText(item.status)}</span>
              {item.status === "draft" && <button type="button" disabled={busy} onClick={(event) => { event.preventDefault(); void start(item); }} className="rounded-md bg-dh-accent px-2.5 py-1 text-xs text-dh-accfg disabled:opacity-50">执行</button>}
              {ACTIVE.has(item.status) && canResume && <button type="button" disabled={busy} onClick={(event) => { event.preventDefault(); void resume(item); }} className="rounded-md border border-dh-bsoft px-2.5 py-1 text-xs text-dh-tsoft disabled:opacity-50">恢复班组</button>}
              {ACTIVE.has(item.status) && <button type="button" disabled={busy} onClick={(event) => { event.preventDefault(); void cancel(item); }} className="rounded-md border border-rose-500/30 px-2.5 py-1 text-xs text-rose-400 disabled:opacity-50">取消编排</button>}
            </summary>
            <div className="mt-3 space-y-2 border-t border-dh-bsoft pt-3">
              {open && <OrchestrationTrace initial={item} onOpenTask={onOpenTask} />}
            </div>
          </details>
        );
      })}
    </section>
  );
}
