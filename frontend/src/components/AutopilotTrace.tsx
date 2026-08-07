import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { GlowDot } from "./ui";

const STAGE: Record<string, { icon: string; name: string }> = {
  analyze: { icon: "🔍", name: "审" },
  fix: { icon: "🔧", name: "修" },
  verify: { icon: "✅", name: "验" },
  review: { icon: "👁", name: "复审" },
  commit: { icon: "📦", name: "提交" },
};
const ORDER = ["analyze", "fix", "verify", "review", "commit"];

function tone(s: string): "ok" | "warn" | "crit" | "idle" {
  return s === "ok" ? "ok" : s === "warn" ? "warn" : s === "fail" ? "crit" : "idle";
}
function dur(a: number, b: number | null): string {
  if (!b) return "…";
  const s = Math.max(0, Math.round(b - a));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m${s % 60}s`;
}

export function AutopilotTrace({ runId, live }: { runId: number; live: boolean }) {
  const [spans, setSpans] = useState<any[]>([]);
  const load = useCallback(() => api.autopilot.spans(runId).then(setSpans).catch(() => {}), [runId]);
  useEffect(() => {
    load();
  }, [load]);
  useEffect(() => {
    if (!live) return;
    const t = setInterval(load, 2000);
    return () => clearInterval(t);
  }, [live, load]);

  if (spans.length === 0)
    return <div className="py-6 text-center text-xs text-slate-400">暂无流水线数据（自愈跑起来后出现）</div>;

  const iters = [...new Set(spans.map((s) => s.iteration))].sort((a, b) => a - b);

  return (
    <div className="space-y-3">
      {iters.map((it) => {
        const bySt = Object.fromEntries(spans.filter((s) => s.iteration === it).map((s) => [s.stage, s]));
        return (
          <div key={it} className="rounded-lg border border-dh-bsoft bg-dh-soft p-2.5">
            <div className="mb-2 text-xs font-medium text-dh-muted">第 {it} 轮</div>
            <div className="flex flex-wrap items-stretch gap-1">
              {ORDER.map((stage, idx) => {
                const s = bySt[stage];
                const meta = STAGE[stage];
                return (
                  <div key={stage} className="flex items-stretch">
                    <div
                      className={`min-w-[92px] rounded-lg border px-2 py-1.5 ${
                        s ? "border-dh-bsoft bg-dh-surface" : "border-dashed border-dh-bsoft bg-transparent opacity-40"
                      }`}
                      title={s?.detail || ""}
                    >
                      <div className="flex items-center gap-1">
                        <GlowDot tone={s ? tone(s.status) : "idle"} size={7} />
                        <span className="text-[11px]">{meta.icon}</span>
                        <span className="text-[11px] font-medium text-dh-tsoft">{meta.name}</span>
                      </div>
                      {s && (
                        <div className="mt-0.5 text-[10px] text-slate-400">
                          {dur(s.started_at, s.ended_at)}
                          {s.tokens > 0 && ` · ${(s.tokens / 1000).toFixed(1)}k`}
                        </div>
                      )}
                      {s?.label && (
                        <div className="mt-0.5 line-clamp-1 text-[10px] text-dh-muted">{s.label}</div>
                      )}
                    </div>
                    {idx < ORDER.length - 1 && (
                      <span className="flex items-center px-0.5 text-slate-300">→</span>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        );
      })}
    </div>
  );
}
