import { useCallback, useEffect, useState } from "react";
import { api, type HarnessCluster, type HarnessMineStatus, type HarnessVersionInfo } from "../api";
import { toast } from "../overlay";
import { useSingleFlight } from "../useSingleFlight";

const CAUSAL_STYLE: Record<HarnessCluster["causal"], { label: string; cls: string }> = {
  harness_gap: { label: "harness 缺口", cls: "bg-emerald-500/15 text-emerald-400" },
  model_limit: { label: "模型局限", cls: "bg-amber-500/15 text-amber-400" },
  env_issue: { label: "环境问题", cls: "bg-sky-500/15 text-sky-400" },
  user_input: { label: "输入不足", cls: "bg-slate-400/15 text-slate-400" },
};

/** harness 演进区块：挖掘失败模式 → 失败簇列表 → 各引擎版本与回滚。
 *  harness_gap 簇会生成提案进上方提案列表（人审采纳才生效），其余簇只展示。 */
export function HarnessEvolution({ onProposalsChanged }: { onProposalsChanged: () => void }) {
  const [status, setStatus] = useState<HarnessMineStatus | null>(null);
  const [clusters, setClusters] = useState<HarnessCluster[]>([]);
  const [versions, setVersions] = useState<HarnessVersionInfo[]>([]);
  const [confirmEngine, setConfirmEngine] = useState<string | null>(null);
  const { busy, run } = useSingleFlight();

  const load = useCallback(async (): Promise<HarnessMineStatus | null> => {
    const [st, cl, vs] = await Promise.all([
      api.proposals.harness.status().catch(() => null),
      api.proposals.harness.clusters().catch(() => [] as HarnessCluster[]),
      api.proposals.harness.versions().catch(() => [] as HarnessVersionInfo[]),
    ]);
    if (st) setStatus(st);
    setClusters(cl);
    setVersions(vs);
    return st;
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // 挖掘运行中轮询；结束时刷新提案列表并报结果
  useEffect(() => {
    if (!status?.running) return;
    const t = window.setInterval(async () => {
      const st = await load();
      if (st && !st.running) {
        onProposalsChanged();
        if (st.last_error) toast(`挖掘失败：${st.last_error}`, "error");
        else toast(`挖掘完成：${st.last_clusters ?? 0} 个失败簇，新增 ${st.last_proposals ?? 0} 条提案`, "success");
      }
    }, 3000);
    return () => window.clearInterval(t);
  }, [status?.running, load, onProposalsChanged]);

  async function mine() {
    await run(async () => {
      try {
        const r = await api.proposals.harness.mine();
        toast(r.started ? "已开始挖掘，逐条分析失败任务，需几分钟" : "已有挖掘正在运行", "info");
        await load();
      } catch (e: any) {
        toast(`挖掘启动失败：${e.message}`, "error");
      }
    });
  }

  async function rollback(engine: string) {
    setConfirmEngine(null);
    await run(async () => {
      try {
        const r = await api.proposals.harness.rollback(engine);
        toast(`${engine} 已回滚到 v${r.active_version}`, "success");
        await load();
      } catch (e: any) {
        toast(`回滚失败：${e.message}`, "error");
      }
    });
  }

  function fmtLast(ts: number | null) {
    if (!ts) return null;
    return new Date(ts * 1000).toLocaleString("zh", { hour12: false });
  }

  const mining = !!status?.running;

  return (
    <div className="shrink-0 rounded-xl border border-dh-bsoft bg-dh-soft p-3">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <span className="text-sm font-medium text-dh-text">Harness 演进</span>
        <button
          className="rounded-lg bg-dh-accent px-3 py-1 text-xs font-medium text-dh-accfg hover:bg-dh-acchov disabled:opacity-50"
          disabled={mining || busy}
          onClick={mine}
        >
          {mining ? "挖掘中…" : "挖掘失败模式"}
        </button>
        <span className="min-w-0 flex-1 truncate text-xs text-slate-400">
          {mining
            ? "正在逐条分析失败任务并聚类，完成后自动刷新"
            : status?.last_run_at
              ? `上次：${fmtLast(status.last_run_at)} · ${status.last_clusters ?? 0} 簇 / 新增 ${status.last_proposals ?? 0} 提案${status.last_error ? ` · 失败：${status.last_error}` : ""}`
              : "分析失败任务 → 聚类失败模式 → harness 缺口自动生成提案（上方列表人审）"}
        </span>
      </div>

      {versions.length > 0 && (
        <div className="mb-2 flex flex-wrap items-center gap-1.5 text-xs">
          {versions.map((v) => (
            <span key={v.engine} className="inline-flex items-center gap-1 rounded bg-dh-s2 px-1.5 py-0.5 text-dh-tsoft">
              {v.engine} · v{v.version}
              <span className="text-slate-500">/{v.versions_total} 版</span>
              {!!v.can_rollback &&
                (confirmEngine === v.engine ? (
                  <button
                    className="rounded bg-rose-500/20 px-1 text-rose-400 hover:bg-rose-500/30 disabled:opacity-50"
                    disabled={busy}
                    onClick={() => rollback(v.engine)}
                  >
                    确认回滚?
                  </button>
                ) : (
                  <button
                    className="rounded px-1 text-slate-400 hover:bg-dh-hover hover:text-dh-tsoft disabled:opacity-50"
                    disabled={busy}
                    onClick={() => setConfirmEngine(v.engine)}
                  >
                    回滚
                  </button>
                ))}
            </span>
          ))}
        </div>
      )}

      {clusters.length > 0 && (
        <div className="max-h-40 space-y-1 overflow-y-auto pr-1">
          {clusters.map((c) => (
            <div key={c.id} className="flex items-center gap-1.5 rounded-lg bg-dh-surface px-2 py-1 text-xs">
              <span className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] ${CAUSAL_STYLE[c.causal]?.cls ?? CAUSAL_STYLE.user_input.cls}`}>
                {CAUSAL_STYLE[c.causal]?.label ?? c.causal}
              </span>
              <span className="shrink-0 rounded bg-dh-s2 px-1 py-0.5 text-[10px] text-dh-tsoft">{c.engine}</span>
              <span className="min-w-0 flex-1 truncate text-dh-tsoft" title={`${c.cause} — ${c.mechanism}`}>
                {c.cause} — {c.mechanism}
              </span>
              <span className="shrink-0 text-slate-400">×{c.support}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
