import { useEffect, useState } from "react";
import { api } from "../api";
import type { Finding, Project } from "../types";
import { AUTO_APPROVE_FLAG } from "../engines";
import { useAvailableEngines } from "../installedEngines";
import { useSingleFlight } from "../useSingleFlight";
import { Modal } from "./Modal";

const SEV: Record<string, string> = {
  error: "bg-rose-500/15 text-rose-400",
  warning: "bg-amber-500/15 text-amber-400",
  info: "bg-dh-s2 text-dh-muted",
};

export function FindingsInbox({
  project,
  onClose,
  onChanged,
}: {
  project: Project;
  onClose: () => void;
  onChanged: () => void;
}) {
  const availableEngines = useAvailableEngines();
  const [findings, setFindings] = useState<Finding[]>([]);
  const [meta, setMeta] = useState<{ last_analyze_at: number | null; audit_skipped: boolean } | null>(null);
  const { busy, run: runAnalyze } = useSingleFlight();
  const { run: runAct } = useSingleFlight();
  const [acting, setActing] = useState<number | null>(null);
  const [autoApprove, setAutoApprove] = useState(true);

  async function load() {
    setFindings(await api.findings(project.id));
    setMeta(await api.findingsMeta(project.id).catch(() => null));
  }
  useEffect(() => {
    load();
  }, [project.id]);

  // 防抖：同一时刻只处理一个 finding 动作，避免快速多点重复请求
  async function act(id: number, fn: () => Promise<unknown>) {
    await runAct(async () => {
      setActing(id);
      try {
        await fn();
        await load();
        onChanged();
      } finally {
        setActing(null);
      }
    });
  }

  async function analyze() {
    await runAnalyze(async () => {
      await api.analyze(project.id);
      await load();
      onChanged();
    });
  }

  function ago(ts: number): string {
    const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
    if (s < 60) return `${s} 秒前`;
    if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
    if (s < 86400) return `${Math.floor(s / 3600)} 小时前`;
    return `${Math.floor(s / 86400)} 天前`;
  }

  return (
    <Modal title={`问题收件箱 · ${project.name}`} onClose={onClose} wide>
      <div className="mb-3 flex items-center gap-3 text-sm">
        <button
          className="shrink-0 whitespace-nowrap rounded-lg bg-teal-600 px-3 py-1.5 font-medium text-white hover:bg-teal-500 disabled:opacity-50"
          disabled={busy}
          onClick={analyze}
        >
          {busy ? "分析中…" : "整体分析（重新扫描）"}
        </button>
        <label
          className={`flex shrink-0 items-center gap-2 rounded-lg border px-2.5 py-1.5 text-sm ${
            autoApprove ? "border-amber-500/40 bg-amber-500/10 text-amber-400" : "border-dh-bsoft text-dh-tsoft"
          }`}
          title={availableEngines.map((item) => `${item}: ${AUTO_APPROVE_FLAG[item]}`).join(" / ")}
        >
          <input
            type="checkbox"
            checked={autoApprove}
            onChange={(e) => setAutoApprove(e.target.checked)}
          />
          <span className="whitespace-nowrap">⚡ 全权限运行（跳过确认）</span>
        </label>
        <span className="min-w-0 flex-1 truncate text-xs text-slate-400">
          自动发现 · 人在环：勾"生成修复任务"后进泳道，等你在终端里 review
        </span>
        {meta?.last_analyze_at && (
          <span className="shrink-0 whitespace-nowrap text-xs text-slate-400">
            上次审查：{ago(meta.last_analyze_at)}
            {meta.audit_skipped && <span className="text-emerald-500"> · 代码未变已跳过</span>}
          </span>
        )}
      </div>
      <div className="space-y-2">
        {findings.length === 0 && (
          <div className="py-6 text-center text-sm text-slate-400">
            暂无待处理问题，点上方「整体分析」扫描
          </div>
        )}
        {findings.map((f) => (
          <div
            key={f.id}
            className={`rounded-xl border p-3 ${
              f.status === "muted" ? "border-dh-bsoft bg-dh-soft" : "border-dh-bsoft bg-dh-surface"
            }`}
          >
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <span className={`rounded px-1.5 text-[10px] font-medium uppercase ${SEV[f.severity] ?? SEV.info}`}>
                {f.severity}
              </span>
              <span className="rounded bg-dh-s2 px-1.5 text-[10px] text-dh-muted">{f.source}</span>
              {f.status === "muted" && (
                <span
                  className="rounded bg-dh-hover px-1.5 text-[10px] text-dh-tsoft"
                  title="反复修复失败, 已降级为仅报告(不再自动修)"
                >
                  🔇 仅报告
                </span>
              )}
              <span className="min-w-0 flex-1 font-medium text-dh-text">{f.title}</span>
              <div className="ml-auto flex gap-1.5">
                {f.status === "muted" ? (
                  <button
                    className="rounded-lg border border-dh-bsoft px-2 py-0.5 text-xs text-dh-tsoft hover:bg-slate-400/20 disabled:opacity-50"
                    disabled={acting != null}
                    onClick={() => act(f.id, () => api.unmuteFinding(f.id))}
                    title="清除失败记忆, 重新允许自动修"
                  >
                    重新启用
                  </button>
                ) : (
                  <button
                    className="rounded-lg bg-violet-500 px-2 py-0.5 text-xs text-white hover:bg-violet-600 disabled:opacity-50"
                    disabled={acting != null}
                    onClick={() => act(f.id, () => api.findingToTask(f.id, "cc", autoApprove))}
                  >
                    → 修复任务
                  </button>
                )}
                <button
                  className="rounded-lg border border-dh-bsoft px-2 py-0.5 text-xs text-dh-tsoft hover:bg-dh-hover disabled:opacity-50"
                  disabled={acting != null}
                  onClick={() => act(f.id, () => api.dismissFinding(f.id))}
                >
                  忽略
                </button>
              </div>
            </div>
            {f.detail && (
              <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-dh-soft p-2.5 font-mono text-[11.5px] leading-relaxed text-dh-tsoft">
                {f.detail}
              </pre>
            )}
          </div>
        ))}
      </div>
    </Modal>
  );
}
