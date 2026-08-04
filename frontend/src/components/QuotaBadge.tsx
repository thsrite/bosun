import { useCallback, useEffect, useState } from "react";
import {
  api,
  type AppSettings,
  type EngineToolInfo,
  type EngineToolUpdateResult,
  type HostMetrics,
} from "../api";
import { Modal } from "./Modal";

type ProviderUsage = {
  available?: boolean;
  error?: string | null;
  plan?: string | null;
  five_hour_pct?: number | null;
  weekly_pct?: number | null;
  five_hour_resets_at?: string | number | null;
  weekly_resets_at?: string | number | null;
  cached_age?: number | null;
};

type QuotaUsage = {
  claude?: ProviderUsage | null;
  codex?: ProviderUsage | null;
  block_pct?: number | null;
};

type EngineToolState = {
  loading?: boolean;
  checking?: boolean;
  updating?: boolean;
  error?: string | null;
  result?: EngineToolUpdateResult | null;
};

type EngineTools = Record<string, EngineToolInfo | null | undefined>;
type EngineToolStates = Record<string, EngineToolState | undefined>;

function dotColor(pct: number | null): string {
  if (pct == null) return "bg-slate-300";
  if (pct >= 85) return "bg-rose-500";
  if (pct >= 60) return "bg-amber-400";
  return "bg-emerald-500";
}

function headerDotColor(pct: number | null): string {
  if (pct == null) return "bg-emerald-500";
  if (pct >= 85) return "bg-rose-500";
  if (pct >= 60) return "bg-amber-400";
  return "bg-emerald-500";
}

function worstPct(q: ProviderUsage | null | undefined): number | null {
  if (!q?.available) return null;
  const vals = [q.five_hour_pct, q.weekly_pct].filter((v): v is number => typeof v === "number");
  return vals.length ? Math.max(...vals) : null;
}

function pctText(pct: number | null | undefined): string {
  return typeof pct === "number" ? `${pct}%` : "?%";
}

/** 5 小时窗为空(如 codex 已取消该限额)时只显示周窗。 */
function hasFiveHour(q: ProviderUsage | null | undefined): boolean {
  return typeof q?.five_hour_pct === "number";
}

function usageText(q: ProviderUsage): string {
  const week = `w${q.weekly_pct ?? "?"}%`;
  return hasFiveHour(q) ? `h${q.five_hour_pct}% ${week}` : week;
}

function usageTitle(q: ProviderUsage): string {
  const week = `周 ${q.weekly_pct ?? "?"}%`;
  return hasFiveHour(q) ? `5小时 ${q.five_hour_pct}% · ${week}` : week;
}

function compactText(q: ProviderUsage | null | undefined): string {
  if (!q) return "…";
  if (!q.available) return "—";
  const worst = worstPct(q);
  return worst == null ? "?%" : `${worst}%`;
}

function pctMetric(
  host: HostMetrics | null,
  key: keyof Pick<HostMetrics, "cpu_load_pct" | "memory_load_pct" | "disk_load_pct">,
): string {
  if (!host) return "…";
  const value = host[key];
  return typeof value === "number" ? `${Math.round(value)}%` : "—";
}

function tempMetric(host: HostMetrics | null): string {
  if (!host) return "…";
  return typeof host.cpu_temp_c === "number" ? `${Math.round(host.cpu_temp_c)}°C` : "—";
}

function hostMetricTone(kind: "temp" | "load", value: number | null | undefined): string {
  if (typeof value !== "number") return "text-slate-500";
  if (kind === "temp") {
    if (value >= 85) return "text-rose-400";
    if (value >= 75) return "text-amber-400";
    return "text-emerald-400";
  }
  if (value >= 90) return "text-rose-400";
  if (value >= 75) return "text-amber-400";
  return "text-emerald-400";
}

function HostMetricStrip({ host }: { host: HostMetrics | null }) {
  const metrics = [
    {
      label: "温度",
      value: tempMetric(host),
      tone: hostMetricTone("temp", host?.cpu_temp_c),
      title: "宿主机 CPU 温度",
    },
    {
      label: "CPU",
      value: pctMetric(host, "cpu_load_pct"),
      tone: hostMetricTone("load", host?.cpu_load_pct),
      title: "宿主机 CPU 负载",
    },
    {
      label: "内存",
      value: pctMetric(host, "memory_load_pct"),
      tone: hostMetricTone("load", host?.memory_load_pct),
      title: "宿主机内存负载",
    },
    {
      label: "硬盘",
      value: pctMetric(host, "disk_load_pct"),
      tone: hostMetricTone("load", host?.disk_load_pct),
      title: "宿主机硬盘负载",
    },
  ];
  return (
    <span className="hidden shrink-0 items-center gap-2 border-l border-emerald-500/40 pl-3 lg:flex">
      {metrics.map((m) => (
        <span key={m.label} className="flex items-baseline gap-1 whitespace-nowrap" title={m.title}>
          <span className="text-slate-400">{m.label}</span>
          <span className={`tabular-nums font-medium ${m.tone}`}>{m.value}</span>
        </span>
      ))}
    </span>
  );
}

function hostTitle(host: HostMetrics | null): string {
  if (!host) return "宿主机指标读取中";
  return `宿主机：温度 ${tempMetric(host)} · CPU ${pctMetric(host, "cpu_load_pct")} · 内存 ${pctMetric(
    host,
    "memory_load_pct",
  )} · 硬盘 ${pctMetric(host, "disk_load_pct")}`;
}

function formatReset(v: string | number | null | undefined): string {
  if (v == null || v === "") return "未知";
  if (typeof v === "number") {
    if (v > 1_000_000_000) {
      const ms = v > 1_000_000_000_000 ? v : v * 1000;
      return new Date(ms).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
    }
    if (v < 60) return `${Math.max(0, Math.round(v))} 秒后`;
    if (v < 3600) return `${Math.ceil(v / 60)} 分钟后`;
    return `${Math.ceil(v / 3600)} 小时后`;
  }
  const d = new Date(v);
  if (!Number.isNaN(d.getTime())) {
    return d.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
  }
  return v;
}

function formatAge(v: number | null | undefined): string {
  if (typeof v !== "number") return "未知";
  if (v < 60) return `${v} 秒`;
  return `${Math.floor(v / 60)} 分 ${v % 60} 秒`;
}

function formatCheckedAt(v: number | null | undefined): string {
  if (typeof v !== "number") return "未检测";
  return new Date(v * 1000).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function versionText(tool: EngineToolInfo | null | undefined): string {
  if (!tool) return "读取中";
  if (tool.version) return tool.version;
  if (tool.error) return "未知";
  return "未安装";
}

function updateStatusText(tool: EngineToolInfo | null | undefined): string {
  if (!tool) return "等待读取";
  if (tool.update_error) return tool.update_error;
  if (tool.checked_at && !tool.latest_version) return "已检查，未返回最新版本";
  if (!tool.latest_version) return "未检测更新";
  if (tool.update_available === true) return `可更新到 ${tool.latest_version}`;
  if (tool.update_available === false) return `已是最新 ${tool.latest_version}`;
  return `最新版本 ${tool.latest_version}`;
}

function updateStatusClass(tool: EngineToolInfo | null | undefined): string {
  if (tool?.update_available === true) return "border-amber-200 bg-amber-50 text-amber-700";
  if (tool?.update_available === false) return "border-emerald-200 bg-emerald-50 text-emerald-700";
  if (tool?.update_error) return "border-slate-200 bg-slate-50 text-slate-500";
  return "border-slate-200 bg-white text-slate-500";
}

function packageText(tool: EngineToolInfo | null | undefined): string {
  if (!tool) return "读取中";
  if (tool.package_name) return tool.package_name;
  if (tool.package_manager === "claude") return "Claude CLI 自更新";
  return "未识别安装包";
}

function checkTitle(tool: EngineToolInfo | null | undefined): string {
  if (tool?.update_error) return tool.update_error;
  if (tool?.latest_package_name || tool?.package_name) return "检测最新版本";
  return "检测更新";
}

function updateTitle(tool: EngineToolInfo | null | undefined): string {
  if (tool?.update_error) return tool.update_error;
  if (tool?.update_command) return `执行 ${tool.update_command}`;
  if (tool?.package_name) return `执行 npm install -g ${tool.package_name}@latest`;
  return "执行在线更新";
}

function Provider({ label, q }: { label: string; q: ProviderUsage | null | undefined }) {
  const worst = worstPct(q);
  const title = !q
    ? `${label} · 正在读取状态`
    : q.available
      ? `${label} · ${usageTitle(q)}${q.plan ? ` · ${q.plan}` : ""}`
      : `${label} · ${q.error || "不可用"}`;
  return (
    <span className="flex items-center gap-1" title={title}>
      <span className={`h-2 w-2 rounded-full ${q?.available ? headerDotColor(worst) : "bg-emerald-300"}`} />
      <span className="text-emerald-700">{label}</span>
      {!q ? (
        <span className="text-emerald-500">…</span>
      ) : q.available ? (
        <span className="tabular-nums text-emerald-700">{usageText(q)}</span>
      ) : (
        <span className="text-slate-400">—</span>
      )}
    </span>
  );
}

function CompactProvider({ label, q }: { label: string; q: ProviderUsage | null | undefined }) {
  const worst = worstPct(q);
  return (
    <span className="flex min-w-0 items-center gap-1">
      <span className={`h-2 w-2 shrink-0 rounded-full ${q?.available ? headerDotColor(worst) : "bg-emerald-300"}`} />
      <span className="shrink-0 text-emerald-700">{label}</span>
      <span className="truncate tabular-nums text-emerald-700">{compactText(q)}</span>
    </span>
  );
}

function UsageBar({ label, pct, reset }: { label: string; pct: number | null | undefined; reset: string | number | null | undefined }) {
  const n = typeof pct === "number" ? pct : 0;
  return (
    <div className="min-w-0">
      <div className="mb-1 flex items-center justify-between gap-2 text-[11px] text-slate-500">
        <span className="min-w-0 truncate" title={`${label} · 重置：${formatReset(reset)}`}>
          {label} · {formatReset(reset)}
        </span>
        <span className="tabular-nums">{pctText(pct)}</span>
      </div>
      <div className="h-1.5 overflow-hidden rounded-full bg-slate-100">
        <div
          className={`h-full rounded-full ${n >= 85 ? "bg-rose-400" : n >= 60 ? "bg-amber-400" : "bg-emerald-400"}`}
          style={{ width: `${Math.min(100, Math.max(0, n))}%` }}
        />
      </div>
    </div>
  );
}

function ToolVersionPanel({
  engine,
  tool,
  state,
  onRefresh,
  onCheck,
  onUpdate,
}: {
  engine: string;
  tool: EngineToolInfo | null | undefined;
  state: EngineToolState | undefined;
  onRefresh: (engine: string) => void;
  onCheck: (engine: string) => void;
  onUpdate: (engine: string) => void;
}) {
  const busy = !!(state?.loading || state?.checking || state?.updating);
  const canCheck = !!tool?.can_check_update && !busy;
  const canUpdate = !!tool?.can_update && !busy;
  const result = state?.result;
  const statusText = state?.checking ? "检测中" : state?.updating ? "更新中" : updateStatusText(tool);

  return (
    <div className="mt-3 border-t border-slate-100 pt-3">
      <div className="flex flex-wrap items-center gap-2">
        <div className="min-w-[180px] flex-1">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <span className="text-xs font-semibold text-slate-700">版本 {versionText(tool)}</span>
            <span
              className={`max-w-[180px] truncate rounded-full border px-2 py-0.5 text-[11px] ${updateStatusClass(tool)}`}
              title={statusText}
            >
              {statusText}
            </span>
          </div>
          <div className="mt-1 truncate text-[11px] text-slate-400" title={tool?.binary || tool?.configured_binary || ""}>
            {packageText(tool)} · {formatCheckedAt(tool?.checked_at)} · {tool?.binary || tool?.configured_binary || "未找到可执行文件"}
          </div>
        </div>
        <div className="flex shrink-0 gap-1.5">
          <button
            type="button"
            className="rounded-md border border-slate-200 bg-white px-2 py-1 text-xs text-slate-600 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-50"
            onClick={() => onRefresh(engine)}
            disabled={busy}
            title="刷新版本"
          >
            {state?.loading ? "刷新中" : "刷新"}
          </button>
          <button
            type="button"
            className="rounded-md border border-slate-200 bg-white px-2 py-1 text-xs text-slate-600 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-50"
            onClick={() => onCheck(engine)}
            disabled={!canCheck}
            title={checkTitle(tool)}
          >
            {state?.checking ? "检测中" : "检测"}
          </button>
          <button
            type="button"
            className="rounded-md border border-emerald-200 bg-emerald-50 px-2 py-1 text-xs text-emerald-700 hover:bg-emerald-100 disabled:cursor-not-allowed disabled:opacity-50"
            onClick={() => onUpdate(engine)}
            disabled={!canUpdate}
            title={updateTitle(tool)}
          >
            {state?.updating ? "更新中" : "更新"}
          </button>
        </div>
      </div>
      {(tool?.error || state?.error || result?.error) && (
        <div className="mt-2 rounded bg-slate-50 px-2 py-1 text-[11px] text-slate-500">
          {state?.error || result?.error || tool?.error}
        </div>
      )}
      {result && (
        <div className={`mt-2 rounded px-2 py-1 text-[11px] ${result.ok ? "bg-emerald-50 text-emerald-700" : "bg-rose-50 text-rose-600"}`}>
          {result.ok
            ? `更新完成：${result.before_version || "未知"} → ${result.after_version || "未知"}`
            : `更新失败${result.exit_code != null ? `（退出码 ${result.exit_code}）` : ""}`}
        </div>
      )}
      {result?.model_options_error && (
        <div className="mt-2 rounded bg-amber-50 px-2 py-1 text-[11px] text-amber-700">
          CLI 已更新，但模型列表刷新失败：{result.model_options_error}
        </div>
      )}
    </div>
  );
}

function ProviderDetail({
  label,
  note,
  q,
  engine,
  tool,
  toolState,
  onRefreshTool,
  onCheckUpdate,
  onUpdateTool,
}: {
  label: string;
  note: string;
  q: ProviderUsage | null | undefined;
  engine: string;
  tool: EngineToolInfo | null | undefined;
  toolState: EngineToolState | undefined;
  onRefreshTool: (engine: string) => void;
  onCheckUpdate: (engine: string) => void;
  onUpdateTool: (engine: string) => void;
}) {
  const worst = worstPct(q);
  return (
    <div className="rounded-lg border border-slate-200 p-3">
      <div className="mb-2 flex items-center gap-2">
        <span className={`h-2.5 w-2.5 rounded-full ${q?.available ? dotColor(worst) : "bg-slate-300"}`} />
        <div className="min-w-0 flex-1">
          <div className="text-sm font-semibold text-slate-900">{label}</div>
          <div className="text-[11px] text-slate-400">{note}</div>
        </div>
        <span className={`rounded-full px-2 py-0.5 text-[11px] ${q?.available ? "bg-emerald-50 text-emerald-600" : "bg-slate-100 text-slate-500"}`}>
          {!q ? "读取中" : q.available ? "可用" : "不可用"}
        </span>
      </div>
      {q?.available ? (
        <div className="space-y-2">
          <div className="text-[11px] text-slate-500">
            {q.plan ? `套餐：${q.plan} · ` : ""}缓存：{formatAge(q.cached_age)}
          </div>
          <div className={`grid gap-2 ${hasFiveHour(q) ? "grid-cols-2" : "grid-cols-1"}`}>
          {hasFiveHour(q) && <UsageBar label="5 小时窗" pct={q.five_hour_pct} reset={q.five_hour_resets_at} />}
          <UsageBar label="7 天窗" pct={q.weekly_pct} reset={q.weekly_resets_at} />
          </div>
        </div>
      ) : (
        <div className="rounded-md bg-slate-50 px-2 py-1.5 text-xs text-slate-500">
          {q?.error || "正在读取状态"}
        </div>
      )}
      <ToolVersionPanel
        engine={engine}
        tool={tool}
        state={toolState}
        onRefresh={onRefreshTool}
        onCheck={onCheckUpdate}
        onUpdate={onUpdateTool}
      />
    </div>
  );
}

export function QuotaBadge({
  onModelOptionsUpdated,
}: {
  onModelOptionsUpdated?: (
    engine: "cc" | "codex",
    options: AppSettings["claude_model_options"],
  ) => void;
}) {
  const [q, setQ] = useState<QuotaUsage | null>(null);
  const [host, setHost] = useState<HostMetrics | null>(null);
  const [open, setOpen] = useState(false);
  const [tools, setTools] = useState<EngineTools>({});
  const [toolStates, setToolStates] = useState<EngineToolStates>({});

  const setToolState = useCallback((engine: string, patch: EngineToolState) => {
    setToolStates((prev) => ({ ...prev, [engine]: { ...(prev[engine] || {}), ...patch } }));
  }, []);

  const loadTool = useCallback(async (engine: string) => {
    setToolState(engine, { loading: true, error: null, result: null });
    try {
      const info = await api.engineTools.get(engine);
      setTools((prev) => ({ ...prev, [engine]: info }));
      setToolState(engine, { loading: false });
    } catch (err) {
      setToolState(engine, { loading: false, error: err instanceof Error ? err.message : String(err) });
    }
  }, [setToolState]);

  const loadTools = useCallback(async () => {
    for (const engine of ["cc", "codex"]) setToolState(engine, { loading: true, error: null, result: null });
    try {
      const info = await api.engineTools.list();
      setTools(info);
      for (const engine of ["cc", "codex"]) setToolState(engine, { loading: false });
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      for (const engine of ["cc", "codex"]) setToolState(engine, { loading: false, error: message });
    }
  }, [setToolState]);

  const checkUpdate = useCallback(async (engine: string) => {
    setToolState(engine, { checking: true, error: null, result: null });
    try {
      const info = await api.engineTools.checkUpdate(engine);
      setTools((prev) => ({ ...prev, [engine]: info }));
      setToolState(engine, { checking: false });
    } catch (err) {
      setToolState(engine, { checking: false, error: err instanceof Error ? err.message : String(err) });
    }
  }, [setToolState]);

  const updateTool = useCallback(async (engine: string) => {
    setToolState(engine, { updating: true, error: null, result: null });
    try {
      const result = await api.engineTools.update(engine);
      const nextInfo = result.after || tools[engine];
      if (nextInfo) setTools((prev) => ({ ...prev, [engine]: nextInfo }));
      if (
        result.ok
        && result.model_options?.length
        && (engine === "cc" || engine === "codex")
      ) {
        onModelOptionsUpdated?.(engine, result.model_options);
      }
      setToolState(engine, { updating: false, result });
    } catch (err) {
      setToolState(engine, { updating: false, error: err instanceof Error ? err.message : String(err) });
    }
  }, [onModelOptionsUpdated, setToolState, tools]);

  useEffect(() => {
    const load = () => api.quota().then(setQ).catch(() => {});
    load();
    const t = setInterval(load, 60000); // 后端有缓存(claude≥185s)，前端 60s 拉一次
    return () => clearInterval(t);
  }, []);

  useEffect(() => {
    const load = () => api.stats.host().then(setHost).catch(() => {});
    load();
    const t = setInterval(load, 10000);
    return () => clearInterval(t);
  }, []);

  useEffect(() => {
    if (open) void loadTools();
  }, [loadTools, open]);

  return (
    <>
      <button
        type="button"
        className="flex min-w-0 flex-1 items-center justify-center gap-2 rounded-lg border border-emerald-200 bg-emerald-50 px-2.5 py-1.5 text-xs hover:bg-emerald-100 md:hidden"
        onClick={() => setOpen(true)}
        title="查看 cc / codex 状态详情"
      >
        <CompactProvider label="cc" q={q?.claude} />
        <span className="shrink-0 text-emerald-200">|</span>
        <CompactProvider label="codex" q={q?.codex} />
      </button>
      <button
        type="button"
        className="hidden max-w-full items-center gap-3 rounded-lg border border-emerald-200 bg-emerald-50 px-2.5 py-1 text-xs hover:bg-emerald-100 md:flex"
        onClick={() => setOpen(true)}
        title={`查看 cc / codex 状态详情；${hostTitle(host)}`}
      >
        <Provider label="Claude" q={q?.claude} />
        <span className="text-emerald-200">|</span>
        <Provider label="Codex" q={q?.codex} />
        <HostMetricStrip host={host} />
      </button>
      {open && (
        <Modal title="cc / Codex 状态" onClose={() => setOpen(false)} wide>
          <div className="space-y-3">
            <div className="grid gap-3 md:grid-cols-2">
              <ProviderDetail
                label="cc"
                note="Claude Code"
                q={q?.claude}
                engine="cc"
                tool={tools.cc}
                toolState={toolStates.cc}
                onRefreshTool={loadTool}
                onCheckUpdate={checkUpdate}
                onUpdateTool={updateTool}
              />
              <ProviderDetail
                label="codex"
                note="Codex CLI"
                q={q?.codex}
                engine="codex"
                tool={tools.codex}
                toolState={toolStates.codex}
                onRefreshTool={loadTool}
                onCheckUpdate={checkUpdate}
                onUpdateTool={updateTool}
              />
            </div>
            <div className="rounded-lg bg-slate-50 px-3 py-2 text-xs text-slate-500">
              周用量执行保护阈值：{q?.block_pct ?? 90}%
            </div>
          </div>
        </Modal>
      )}
    </>
  );
}
