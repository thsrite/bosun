import { useCallback, useEffect, useState } from "react";
import { api, type ProposalAction, type ProposalItem, type ReflectionSettings } from "../api";
import { toast } from "../overlay";
import { useSingleFlight } from "../useSingleFlight";
import { Modal } from "./Modal";

export function ProposalsDialog({ onClose }: { onClose: () => void }) {
  const [items, setItems] = useState<ProposalItem[]>([]);
  const { busy, run: runReflect } = useSingleFlight();
  const { run: runAct } = useSingleFlight();
  const { busy: savingSettings, run: runSettings } = useSingleFlight();
  const [acting, setActing] = useState<number | null>(null);
  const [settings, setSettings] = useState<ReflectionSettings | null>(null);
  const [manualReflecting, setManualReflecting] = useState(false);
  const [dismissId, setDismissId] = useState<number | null>(null);
  const [dismissReason, setDismissReason] = useState("");

  const load = useCallback(() => api.proposals.list().then(setItems).catch(() => {}), []);
  const loadSettings = useCallback(() => api.proposals.getSettings().then(setSettings).catch(() => {}), []);
  useEffect(() => {
    load();
    loadSettings();
  }, [load, loadSettings]);

  const reflectionRunning = busy || manualReflecting || !!settings?.running;

  useEffect(() => {
    if (!manualReflecting && !settings?.running) return;
    let cancelled = false;
    async function poll() {
      const next = await api.proposals.getSettings().catch(() => null);
      if (cancelled || !next) return;
      setSettings(next);
      await load();
      if (!next.running) setManualReflecting(false);
    }
    void poll();
    const t = window.setInterval(poll, 3000);
    return () => {
      cancelled = true;
      window.clearInterval(t);
    };
  }, [load, manualReflecting, settings?.running]);

  // 防抖：同一时刻只处理一个提案动作
  async function act(id: number, fn: () => Promise<unknown>) {
    await runAct(async () => {
      setActing(id);
      try {
        await fn();
        await load();
      } finally {
        setActing(null);
      }
    });
  }

  async function reflect() {
    await runReflect(async () => {
      setManualReflecting(true);
      try {
        const r = await api.proposals.reflect();
        await loadSettings();
        await load();
        toast(r.started ? "已开始反思，完成后会自动刷新提案" : "已有反思正在运行", "info");
      } catch (e: any) {
        setManualReflecting(false);
        toast(`反思失败：${e.message}`, "error");
      }
    });
  }

  async function saveSettings(next: Partial<ReflectionSettings>) {
    const merged = { ...(settings ?? {
      auto_enabled: true,
      interval_minutes: 1440,
      min_pending_gap: 5,
      last_run_at: null,
      last_skip_reason: "",
    }), ...next };
    await runSettings(async () => {
      const saved = await api.proposals.updateSettings({
        auto_enabled: merged.auto_enabled,
        interval_minutes: merged.interval_minutes,
        min_pending_gap: merged.min_pending_gap,
      });
      setSettings(saved);
      toast("自动反思设置已更新", "success");
    });
  }

  function fmtLast(ts: number | null | undefined) {
    if (!ts) return "尚未运行";
    return new Date(ts * 1000).toLocaleString("zh", { hour12: false });
  }

  function actionLabel(action: ProposalAction | null) {
    if (!action) return "可转跟进任务";
    if (action.type === "create_task") return "可创建任务";
    return `可应用：${action.type}`;
  }

  // 采纳前让用户看到具体会改什么, 避免盲批配置变更
  function actionDetail(action: ProposalAction | null): string | null {
    if (!action) return null;
    if (action.type === "set_setting") return `${String(action.key)} = ${JSON.stringify(action.value)}`;
    if (action.type === "set_policy")
      return `策略 #${String(action.policy_id)}.${String(action.field)} = ${JSON.stringify(action.value)}`;
    if (action.type === "create_task")
      return `${String(action.engine ?? "codex")} · ${String(action.title ?? action.prompt ?? "").slice(0, 80)}`;
    return JSON.stringify(action);
  }

  async function confirmDismiss(id: number) {
    const reason = dismissReason.trim();
    await act(id, () => api.proposals.dismiss(id, reason));
    setDismissId(null);
    setDismissReason("");
  }

  return (
    <Modal title="自进化提案（反思循环）" onClose={onClose} wide>
      <div className="flex max-h-[calc(100dvh-8.5rem)] min-h-0 flex-col gap-3 text-sm">
        <div className="flex shrink-0 items-center gap-2">
          <button
            className="rounded-lg bg-dh-accent px-3 py-1.5 font-medium text-dh-accfg hover:bg-dh-acchov disabled:opacity-50"
            disabled={reflectionRunning}
            onClick={reflect}
          >
            {reflectionRunning ? "反思运行中…" : "跑一次反思"}
          </button>
          <span className="text-xs text-slate-400">
            让 cc 读 Bosun 运行数据 → 产出改进提案，你采纳才生效（白名单动作可自动应用）
          </span>
        </div>

        {reflectionRunning && (
          <div className="shrink-0 rounded-lg border border-slate-400/30 bg-slate-400/10 px-3 py-2 text-xs text-dh-tsoft">
            正在调用 cc 读取运行数据并生成提案，通常需要几十秒；完成后列表会自动刷新。
          </div>
        )}

        {settings && (
          <div className="shrink-0 rounded-xl border border-dh-bsoft bg-dh-soft p-3">
            <div className="mb-2 flex items-center gap-2">
              <label className="flex items-center gap-2 text-sm font-medium text-dh-text">
                <input
                  type="checkbox"
                  checked={settings.auto_enabled}
                  disabled={savingSettings}
                  onChange={(e) => saveSettings({ auto_enabled: e.target.checked })}
                />
                自动反思
              </label>
              <span className="text-xs text-slate-400">
                定时读取系统诊断数据，生成待你采纳的优化提案
              </span>
            </div>
            <div className="grid grid-cols-1 gap-2 text-xs text-dh-muted sm:grid-cols-3">
              <label className="flex items-center gap-2">
                间隔(小时)
                <input
                  className="w-20 rounded-md border border-dh-bsoft bg-dh-surface px-2 py-1 text-dh-tsoft"
                  type="number"
                  min={1}
                  max={168}
                  value={Math.round(settings.interval_minutes / 60)}
                  disabled={savingSettings}
                  onChange={(e) => saveSettings({ interval_minutes: Math.max(1, Number(e.target.value) || 1) * 60 })}
                />
              </label>
              <label className="flex items-center gap-2">
                Pending 阈值
                <input
                  className="w-20 rounded-md border border-dh-bsoft bg-dh-surface px-2 py-1 text-dh-tsoft"
                  type="number"
                  min={1}
                  max={50}
                  value={settings.min_pending_gap}
                  disabled={savingSettings}
                  onChange={(e) => saveSettings({ min_pending_gap: Math.max(1, Number(e.target.value) || 1) })}
                />
              </label>
              <div className="min-w-0 truncate">
                上次：{fmtLast(settings.last_run_at)}
                {settings.last_skip_reason && <span className="ml-1 text-amber-400">({settings.last_skip_reason})</span>}
                {typeof settings.last_new_count === "number" && (
                  <span className="ml-1 text-slate-400">新增 {settings.last_new_count}</span>
                )}
              </div>
            </div>
          </div>
        )}

        <div className="min-h-0 flex-1 space-y-2 overflow-y-auto pr-1">
          {items.length > 0 && (
            <div className="sticky top-0 z-10 flex items-center justify-between border-b border-dh-bsoft bg-dh-surface py-1 text-xs text-slate-400 backdrop-blur">
              <span>待处理提案 {items.length} 条</span>
              <span>列表可滚动</span>
            </div>
          )}
          {items.length === 0 && (
            <div className="py-6 text-center text-sm text-slate-400">
              {reflectionRunning ? "正在生成提案…" : "暂无待处理提案，点上方「跑一次反思」"}
            </div>
          )}
          {items.map((p) => (
            <div key={p.id} className="rounded-xl border border-dh-bsoft bg-dh-surface p-3">
              <div className="flex items-start gap-2">
                <div className="min-w-0 flex-1">
                  <div className="font-medium text-dh-text">{p.title}</div>
                  {p.rationale && (
                    <div className="mt-1 max-h-20 overflow-y-auto pr-1 text-xs leading-relaxed text-dh-muted">
                      {p.rationale}
                    </div>
                  )}
                  <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                    {p.action ? (
                      <code className="inline-block rounded bg-slate-400/10 px-1.5 py-0.5 text-[10px] text-dh-tsoft">
                        {actionLabel(p.action)}
                      </code>
                    ) : (
                      <span className="inline-block text-[10px] text-slate-400">{actionLabel(null)}</span>
                    )}
                    {p.task && (
                      <span className="inline-block rounded bg-dh-s2 px-1.5 py-0.5 text-[10px] text-dh-tsoft">
                        任务 #{p.task.id} · {p.task.status}
                      </span>
                    )}
                  </div>
                  {actionDetail(p.action) && (
                    <code className="mt-1 block overflow-x-auto whitespace-pre rounded bg-dh-soft px-2 py-1 text-[10px] text-dh-tsoft">
                      {actionDetail(p.action)}
                    </code>
                  )}
                </div>
                <div className="flex shrink-0 gap-1.5">
                  <button
                    className="rounded-lg bg-emerald-500 px-2.5 py-1 text-xs font-medium text-white hover:bg-emerald-600 disabled:opacity-50"
                    disabled={acting != null}
                    onClick={() =>
                      act(p.id, async () => {
                        const r = await api.proposals.apply(p.id);
                        toast(
                          r.task_id
                            ? `已创建 draft 任务 #${r.task_id}`
                            : r.applied
                              ? `已应用：${r.note}`
                              : `已采纳（${r.note}）`,
                          "success",
                        );
                      })
                    }
                  >
                    采纳
                  </button>
                  <button
                    className="rounded-lg border border-dh-bsoft px-2.5 py-1 text-xs text-dh-tsoft hover:bg-dh-hover disabled:opacity-50"
                    disabled={acting != null}
                    onClick={() => {
                      setDismissReason("");
                      setDismissId((cur) => (cur === p.id ? null : p.id));
                    }}
                  >
                    忽略
                  </button>
                </div>
              </div>
              {dismissId === p.id && (
                <div className="mt-2 flex items-center gap-1.5 border-t border-dh-bsoft pt-2">
                  <input
                    className="min-w-0 flex-1 rounded-md border border-dh-bsoft bg-dh-surface px-2 py-1 text-xs text-dh-tsoft"
                    placeholder="忽略理由（可选，会回流给下一轮反思，避免重复提出）"
                    value={dismissReason}
                    disabled={acting != null}
                    autoFocus
                    onChange={(e) => setDismissReason(e.target.value)}
                    onKeyDown={(e) => e.key === "Enter" && confirmDismiss(p.id)}
                  />
                  <button
                    className="shrink-0 rounded-lg border border-dh-bsoft px-2.5 py-1 text-xs text-dh-tsoft hover:bg-dh-hover disabled:opacity-50"
                    disabled={acting != null}
                    onClick={() => confirmDismiss(p.id)}
                  >
                    确认忽略
                  </button>
                </div>
              )}
            </div>
          ))}
        </div>
      </div>
    </Modal>
  );
}
