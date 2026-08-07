import { useEffect, useState, type ReactNode } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api, type HostMetrics } from "../api";
import { useEngineVisible } from "../installedEngines";
import { CHART, PIE_COLORS } from "../theme";
import type { Project } from "../types";
import { GlowDot } from "./ui";

function ago(ts: number): string {
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (s < 60) return `${s}秒前`;
  if (s < 3600) return `${Math.floor(s / 60)}分钟前`;
  if (s < 86400) return `${Math.floor(s / 3600)}小时前`;
  return `${Math.floor(s / 86400)}天前`;
}

type Tone = "ok" | "warn" | "crit" | "idle";
function projectTone(p: Project): Tone {
  if ((p.by_status?.failed ?? 0) > 0) return "crit";
  if (p.open_findings > 0) return "warn";
  if (p.running > 0 || (p.by_status?.done ?? 0) > 0) return "ok";
  return "idle";
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
  if (typeof value !== "number") return "text-dh-muted";
  if (kind === "temp") {
    if (value >= 85) return "text-rose-400";
    if (value >= 75) return "text-amber-400";
    return "text-emerald-400";
  }
  if (value >= 90) return "text-rose-400";
  if (value >= 75) return "text-amber-400";
  return "text-emerald-400";
}

const tooltipStyle = {
  background: "#101722",
  border: "1px solid #253448",
  borderRadius: 10,
  color: "#e5edf7",
  fontSize: 12,
  boxShadow: "0 12px 28px rgba(0,0,0,.35)",
};
const tooltipTextStyle = { color: "#e5edf7" };
const GRID_STROKE = "#1d2a3a";
const AXIS_STROKE = "#94a3b8";
const legendStyle = { fontSize: 11, color: "#94a3b8" };
// recharts v3 起 Tooltip 默认 itemSorter="name"（按名称排序），会把「创建/完成/失败」
// 打乱成「创建/失败/完成」。返回常量让排序退化为稳定排序，保持声明顺序。
const keepSeriesOrder = () => 0;

function formatTokenCompact(value: number, fractionDigits = 0): string {
  if (!Number.isFinite(value)) return "—";
  const units = ["", "k", "M", "B", "T"];
  const abs = Math.abs(value);
  let unitIndex = abs < 1000
    ? 0
    : Math.min(Math.floor(Math.log10(abs) / 3), units.length - 1);
  if (unitIndex === 0) return String(Math.round(value));
  let scaled = value / 1000 ** unitIndex;
  // 999,999 这类边界值四舍五入后应显示 1M，而不是 1000k。
  if (Math.abs(Number(scaled.toFixed(fractionDigits))) >= 1000 && unitIndex < units.length - 1) {
    unitIndex += 1;
    scaled = value / 1000 ** unitIndex;
  }
  return `${scaled.toFixed(fractionDigits)}${units[unitIndex]}`;
}

function formatTokenTooltip(value: unknown): string {
  return `${formatTokenCompact(Number(value), 1)} tok`;
}

function TokenIcon() {
  return (
    <svg
      aria-hidden="true"
      className="h-[18px] w-[18px] shrink-0 text-amber-400"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <circle cx="12" cy="12" r="9" />
      <path d="M9 8h6M12 8v8M9.5 16h5" />
    </svg>
  );
}

function ChartPanel({ title, icon, children }: { title: string; icon: ReactNode; children: ReactNode }) {
  return (
    <div className="card flex-1 p-4">
      <div className="mb-3 flex items-center gap-2 text-[15px] font-semibold text-dh-text">
        <span className="inline-flex h-5 w-5 items-center justify-center" aria-hidden="true">{icon}</span>
        {title}
      </div>
      {children}
    </div>
  );
}

export function HomeView({
  projects,
  onOpen,
  tab,
  onAddProject,
  onProposals,
  onDeleteProject,
}: {
  projects: Project[];
  onOpen: (id: number) => void;
  tab: "projects" | "stats" | "delivery";
  onAddProject: () => void;
  onProposals: () => void;
  onDeleteProject: (project: Project) => void;
}) {
  const [ov, setOv] = useState<any>(null);
  const [engines, setEngines] = useState<any>({});
  const [findings, setFindings] = useState<any>({ by_source: {} });
  const [timeline, setTimeline] = useState<any[]>([]);
  const [tokens, setTokens] = useState<any>({ by_project: [], by_engine: {}, total: 0 });
  const [tokTl, setTokTl] = useState<any[]>([]);
  const [dashboard, setDashboard] = useState<any>(null);
  const [q, setQ] = useState<any>(null);
  const [host, setHost] = useState<HostMetrics | null>(null);
  // 没装的引擎不展示配额卡片
  const showClaude = useEngineVisible("cc");
  const showCodex = useEngineVisible("codex");

  useEffect(() => {
    // 统一 dashboard 是统计/诊断的唯一数据源；概览条也从这里派生，避免重复 SQL/API。
    api.stats.dashboard(30).then((d) => {
      setDashboard(d);
      setOv(d.summary);
      setHost(d.host ?? null);
      if (tab === "stats" || tab === "delivery") {
        setEngines(Object.fromEntries((d.engine_quality ?? []).map((e: any) => [e.engine, e.tasks])));
        setFindings({
          by_source: d.finding_health?.by_source ?? {},
          by_severity: d.finding_health?.by_severity ?? {},
          by_status: d.finding_health?.by_status ?? {},
        });
        setTimeline(d.throughput ?? []);
        setTokens(d.token_economics ?? { by_project: [], by_engine: {}, total: 0 });
        setTokTl(d.token_economics?.timeline ?? []);
      }
    }).catch(() => {});
    api.quota().then(setQ).catch(() => {});
  }, [projects, tab]);

  useEffect(() => {
    const load = () => api.stats.host().then(setHost).catch(() => {});
    const t = setInterval(load, 10000);
    return () => clearInterval(t);
  }, []);

  const running = projects.reduce((a, p) => a + p.running, 0);
  const manualOpen = projects.reduce((a, p) => a + (p.manual_open ?? 0), 0);
  const autoOpen = projects.reduce((a, p) => a + (p.auto_open ?? 0), 0);
  const fixed = projects.reduce((a, p) => a + (p.fixed_findings ?? 0), 0);
  const autopilotRunning = projects.filter((p) => p.autopilot?.status === "running").length;
  // 健康 = 100 起步，仅对有问题(crit/warn)的项目扣分；无活动(idle)不算不健康
  const critCount = projects.filter((p) => projectTone(p) === "crit").length;
  const warnCount = projects.filter((p) => projectTone(p) === "warn").length;
  const computedHealth = projects.length
    ? Math.max(0, Math.round(100 - (critCount * 100 + warnCount * 40) / projects.length))
    : 100;
  const health = dashboard?.summary?.health_score ?? computedHealth;
  const healthTone: Tone = health >= 80 ? "ok" : health >= 50 ? "warn" : "crit";
  const enginePie = Object.entries(engines).map(([name, value]) => ({ name, value }));
  const sourceBar = Object.entries(findings.by_source).map(([name, value]) => ({ name, value }));
  const tl = timeline.map((d) => ({
    date: new Date(d.date * 1000).toLocaleDateString("zh", { month: "numeric", day: "numeric" }),
    created: d.created,
    done: d.done,
    failed: d.failed,
  }));
  const insights = dashboard?.insights ?? [];

  return (
    <div className="space-y-5 px-4 py-5 md:space-y-6 md:px-8 md:py-6">
      {/* 健康概览条：分隔单元格，全部顶对齐 */}
      <div className="card grid grid-cols-3 items-stretch divide-dh-bsoft md:flex md:flex-wrap md:divide-x">
        <div className="flex items-center gap-2.5 px-3 py-2.5 md:px-5 md:py-3.5">
          <GlowDot tone={healthTone} size={10} />
          <div>
            <div className="text-xs text-slate-400">总体健康</div>
            <div className="text-lg font-semibold text-dh-text md:text-xl">{health}%</div>
          </div>
        </div>
        <Stat label="项目" value={projects.length} />
        <Stat label="任务总数" value={ov?.total_tasks ?? "—"} />
        <Stat label="正在运行" value={running} tone="text-emerald-300" />
        <Stat label="人工发现" value={manualOpen} tone="text-amber-400" />
        <Stat label="自动发现" value={autoOpen} tone="text-violet-300" />
        <Stat label="已修复" value={fixed} tone="text-emerald-300" />
        <Stat label="成功率" value={ov ? `${ov.success_rate}%` : "—"} />
        <div className="col-span-3 mt-1 flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 border-t border-dh-bsoft px-3 pb-2.5 pt-2 text-xs text-slate-400 md:ml-auto md:mt-0 md:border-0 md:px-5 md:pb-0 md:pt-0">
          {autopilotRunning > 0 && (
            <span className="shrink-0 font-medium text-emerald-500">🤖 {autopilotRunning} 自愈中</span>
          )}
          <span className="shrink-0">平均时长 {ov ? `${ov.avg_duration_sec}s` : "—"}</span>
          {ov?.deleted_tasks > 0 && <span className="shrink-0">累计已删 {ov.deleted_tasks}</span>}
          <HostMetricsLine host={host} />
        </div>
      </div>

      {tab === "stats" && (
        <>
      {insights.length > 0 && (
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
          {insights.slice(0, 6).map((item: any, i: number) => (
            <div
              key={`${item.title}-${i}`}
              className={`card border-l-4 p-4 ${
                item.severity === "critical"
                  ? "border-l-rose-500"
                  : item.severity === "warning"
                    ? "border-l-amber-400"
                    : "border-l-sky-400"
              }`}
            >
              <div className="text-sm font-semibold text-dh-text">{item.title}</div>
              <div className="mt-1 text-xs leading-relaxed text-dh-muted">{item.detail}</div>
              {item.action && (
                <div className="mt-2 rounded-md bg-dh-soft px-2 py-1.5 text-xs text-dh-tsoft">
                  初步方案：{item.action}
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {/* 任务趋势 */}
      <div className="flex flex-col gap-4 md:flex-row">
        <ChartPanel title="任务趋势（近 14 天）" icon="⚡">
          <ResponsiveContainer width="100%" height={190}>
            <LineChart data={tl}>
              <CartesianGrid strokeDasharray="3 3" stroke={GRID_STROKE} />
              <XAxis dataKey="date" fontSize={11} stroke={AXIS_STROKE} tickLine={false} axisLine={false} />
              <YAxis fontSize={11} stroke={AXIS_STROKE} allowDecimals={false} tickLine={false} axisLine={false} />
              <Tooltip contentStyle={tooltipStyle} itemStyle={tooltipTextStyle} labelStyle={tooltipTextStyle} itemSorter={keepSeriesOrder} />
              <Line type="monotone" dataKey="created" name="创建" stroke={CHART.blue} strokeWidth={2} dot={false} />
              <Line type="monotone" dataKey="done" name="完成" stroke={CHART.mint} strokeWidth={2} dot={false} />
              <Line type="monotone" dataKey="failed" name="失败" stroke={CHART.coral} strokeWidth={2} dot={false} />
            </LineChart>
          </ResponsiveContainer>
        </ChartPanel>
      </div>

      {/* 引擎 / 问题 */}
      <div className="flex flex-col gap-4 md:flex-row">
        <ChartPanel title="引擎用量" icon="🔧">
          <ResponsiveContainer width="100%" height={150}>
            <PieChart>
              <Pie data={enginePie} dataKey="value" nameKey="name" innerRadius={38} outerRadius={62} paddingAngle={2}>
                {enginePie.map((_, i) => (
                  <Cell key={i} fill={PIE_COLORS[i % PIE_COLORS.length]} />
                ))}
              </Pie>
              <Tooltip contentStyle={tooltipStyle} itemStyle={tooltipTextStyle} labelStyle={tooltipTextStyle} />
            </PieChart>
          </ResponsiveContainer>
        </ChartPanel>
        <ChartPanel title="问题来源分布" icon="📊">
          <ResponsiveContainer width="100%" height={150}>
            <BarChart data={sourceBar}>
              <CartesianGrid strokeDasharray="3 3" stroke={GRID_STROKE} />
              <XAxis dataKey="name" fontSize={11} stroke={AXIS_STROKE} tickLine={false} axisLine={false} />
              <YAxis fontSize={11} stroke={AXIS_STROKE} allowDecimals={false} tickLine={false} axisLine={false} />
              <Tooltip contentStyle={tooltipStyle} itemStyle={tooltipTextStyle} labelStyle={tooltipTextStyle} />
              <Bar dataKey="value" fill={CHART.yellow} radius={[4, 4, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </ChartPanel>
      </div>

      {/* 行3：token 排行(按项目) + 按引擎 */}
      <div className="flex flex-col gap-4 md:flex-row">
        <ChartPanel title={`token 消耗排行（按项目，共 ${formatTokenCompact(tokens.total, 1)}）`} icon={<TokenIcon />}>
          {tokens.by_project.length === 0 ? (
            <div className="py-8 text-center text-xs text-slate-400">
              暂无 token 记录（任务/自愈跑完并落盘后才有）
            </div>
          ) : (
            <ResponsiveContainer width="100%" height={Math.max(120, tokens.by_project.length * 34)}>
              <BarChart data={tokens.by_project} layout="vertical" margin={{ left: 10, right: 20 }}>
                <CartesianGrid strokeDasharray="3 3" stroke={GRID_STROKE} horizontal={false} />
                <XAxis
                  type="number"
                  fontSize={11}
                  stroke={AXIS_STROKE}
                  tickLine={false}
                  axisLine={false}
                  tickFormatter={(v) => formatTokenCompact(Number(v))}
                />
                <YAxis type="category" dataKey="name" fontSize={11} stroke={AXIS_STROKE} width={100} tickLine={false} axisLine={false} />
                <Tooltip contentStyle={tooltipStyle} itemStyle={tooltipTextStyle} labelStyle={tooltipTextStyle} formatter={formatTokenTooltip} itemSorter={keepSeriesOrder} />
                <Legend wrapperStyle={legendStyle} />
                <Bar dataKey="task_tokens" name="任务" stackId="a" fill={CHART.blue} />
                <Bar dataKey="autopilot_tokens" name="自愈" stackId="a" fill={CHART.mint} radius={[0, 4, 4, 0]} />
              </BarChart>
            </ResponsiveContainer>
          )}
        </ChartPanel>
        <div className="w-full shrink-0 md:w-64">
          <ChartPanel title="token 按引擎" icon={<TokenIcon />}>
            <ResponsiveContainer width="100%" height={160}>
              <PieChart>
                <Pie
                  data={Object.entries(tokens.by_engine).map(([name, value]) => ({ name, value }))}
                  dataKey="value"
                  nameKey="name"
                  innerRadius={38}
                  outerRadius={62}
                  paddingAngle={2}
                >
                  {Object.keys(tokens.by_engine).map((_, i) => (
                    <Cell key={i} fill={PIE_COLORS[i % PIE_COLORS.length]} />
                  ))}
                </Pie>
                <Tooltip contentStyle={tooltipStyle} itemStyle={tooltipTextStyle} labelStyle={tooltipTextStyle} formatter={formatTokenTooltip} itemSorter={keepSeriesOrder} />
                <Legend wrapperStyle={legendStyle} />
              </PieChart>
            </ResponsiveContainer>
          </ChartPanel>
        </div>
      </div>

      {/* 行4：token 每日趋势 */}
      <ChartPanel title="token 每日消耗趋势" icon="📈">
        <ResponsiveContainer width="100%" height={170}>
          <BarChart
            data={tokTl.map((d) => ({
              date: new Date(d.date * 1000).toLocaleDateString("zh", { month: "numeric", day: "numeric" }),
              任务: d.task_tokens,
              自愈: d.autopilot_tokens,
            }))}
          >
            <CartesianGrid strokeDasharray="3 3" stroke={GRID_STROKE} />
            <XAxis dataKey="date" fontSize={11} stroke={AXIS_STROKE} tickLine={false} axisLine={false} />
            <YAxis fontSize={11} stroke={AXIS_STROKE} tickLine={false} axisLine={false} tickFormatter={(v) => formatTokenCompact(Number(v))} />
            <Tooltip contentStyle={tooltipStyle} itemStyle={tooltipTextStyle} labelStyle={tooltipTextStyle} formatter={formatTokenTooltip} itemSorter={keepSeriesOrder} />
            <Legend wrapperStyle={legendStyle} />
            <Bar dataKey="任务" stackId="a" fill={CHART.blue} />
            <Bar dataKey="自愈" stackId="a" fill={CHART.mint} radius={[4, 4, 0, 0]} />
          </BarChart>
        </ResponsiveContainer>
      </ChartPanel>
        </>
      )}

      {tab === "delivery" && (
        <>
          {/* 配额余量 */}
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
            {[
              ...(showClaude ? [["Claude", q?.claude]] : []),
              ...(showCodex ? [["Codex", q?.codex]] : []),
            ].map(([name, u]: any) => (
              <div key={name} className="card p-4">
                <div className="mb-3 flex items-center gap-2 text-sm font-semibold text-dh-text">
                  <GlowDot
                    tone={
                      !u?.available
                        ? "idle"
                        : Math.max(u.five_hour_pct ?? 0, u.weekly_pct ?? 0) >= 85
                          ? "crit"
                          : Math.max(u.five_hour_pct ?? 0, u.weekly_pct ?? 0) >= 60
                            ? "warn"
                            : "ok"
                    }
                  />
                  {name} 配额{u?.plan ? ` · ${u.plan}` : ""}
                </div>
                {u?.available ? (
                  <div className="space-y-2.5">
                    {/* 5 小时窗为空(如 codex 已取消该限额)时不展示 */}
                    {[
                      ["5 小时窗", u.five_hour_pct],
                      ["7 天窗", u.weekly_pct],
                    ]
                      .filter(([lbl, pct]: any) => lbl !== "5 小时窗" || typeof pct === "number")
                      .map(([lbl, pct]: any) => (
                      <div key={lbl}>
                        <div className="mb-1 flex justify-between text-[11px] text-dh-muted">
                          <span>{lbl}</span>
                          <span className="tabular-nums">{pct ?? "?"}%</span>
                        </div>
                        <div className="h-2 overflow-hidden rounded-full bg-dh-s2">
                          <div
                            className={`h-full rounded-full ${
                              (pct ?? 0) >= 85 ? "bg-rose-400" : (pct ?? 0) >= 60 ? "bg-amber-400" : "bg-emerald-400"
                            }`}
                            style={{ width: `${Math.min(100, pct ?? 0)}%` }}
                          />
                        </div>
                      </div>
                    ))}
                  </div>
                ) : (
                  <div className="text-xs text-slate-400">{u?.error || "用量不可用"}</div>
                )}
              </div>
            ))}
          </div>

          {/* 任务吞吐 + token 消耗 */}
          <div className="flex flex-col gap-4 md:flex-row">
            <ChartPanel title="任务吞吐（每日完成/失败）" icon="🚀">
              <ResponsiveContainer width="100%" height={180}>
                <BarChart data={tl}>
                  <CartesianGrid strokeDasharray="3 3" stroke={GRID_STROKE} />
                  <XAxis dataKey="date" fontSize={11} stroke={AXIS_STROKE} tickLine={false} axisLine={false} />
                  <YAxis fontSize={11} stroke={AXIS_STROKE} allowDecimals={false} tickLine={false} axisLine={false} />
                  <Tooltip contentStyle={tooltipStyle} itemStyle={tooltipTextStyle} labelStyle={tooltipTextStyle} itemSorter={keepSeriesOrder} />
                  <Legend wrapperStyle={legendStyle} />
                  <Bar dataKey="done" name="完成" stackId="a" fill={CHART.mint} />
                  <Bar dataKey="failed" name="失败" stackId="a" fill={CHART.coral} radius={[4, 4, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </ChartPanel>
            <ChartPanel title={`token 消耗（共 ${formatTokenCompact(tokens.total, 1)}）`} icon={<TokenIcon />}>
              <ResponsiveContainer width="100%" height={180}>
                <BarChart
                  data={tokTl.map((d) => ({
                    date: new Date(d.date * 1000).toLocaleDateString("zh", { month: "numeric", day: "numeric" }),
                    任务: d.task_tokens,
                    自愈: d.autopilot_tokens,
                  }))}
                >
                  <CartesianGrid strokeDasharray="3 3" stroke={GRID_STROKE} />
                  <XAxis dataKey="date" fontSize={11} stroke={AXIS_STROKE} tickLine={false} axisLine={false} />
                  <YAxis fontSize={11} stroke={AXIS_STROKE} tickLine={false} axisLine={false} tickFormatter={(v) => formatTokenCompact(Number(v))} />
                  <Tooltip contentStyle={tooltipStyle} itemStyle={tooltipTextStyle} labelStyle={tooltipTextStyle} formatter={formatTokenTooltip} itemSorter={keepSeriesOrder} />
                  <Legend wrapperStyle={legendStyle} />
                  <Bar dataKey="任务" stackId="a" fill={CHART.blue} />
                  <Bar dataKey="自愈" stackId="a" fill={CHART.mint} radius={[4, 4, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </ChartPanel>
          </div>
        </>
      )}

      {/* 项目网格 */}
      {tab === "projects" && (
      <div>
        <div className="mb-3 flex items-center gap-2">
          <span className="text-sm font-semibold text-dh-tsoft">项目</span>
          <span className="text-xs text-slate-400">{projects.length}</span>
          <button
            onClick={onProposals}
            className="ml-auto rounded-lg border border-dh-bsoft px-3 py-1.5 text-sm text-dh-tsoft hover:bg-dh-hover"
            title="自进化提案"
          >
            自进化提案
          </button>
          <button
            onClick={onAddProject}
            className="rounded-lg bg-teal-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-teal-500"
          >
            ＋<span className="hidden sm:inline"> 添加 / 扫描路径</span>
          </button>
        </div>
        {projects.length === 0 && (
          <div className="card py-16 text-center text-slate-400">
            还没有项目。点右上「＋ 添加 / 扫描路径」浏览目录导入本机 git 仓库。
          </div>
        )}
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 md:grid-cols-3 xl:grid-cols-4">
          {/* 有活跃任务（运行、待输入或排队）的项目排前面，组内保持原顺序（sort 稳定） */}
          {[...projects]
            .sort((a, b) =>
              Number(b.running + (b.by_status?.queued ?? 0) > 0)
              - Number(a.running + (a.by_status?.queued ?? 0) > 0),
            )
            .map((p) => {
            const draft = p.by_status?.draft ?? 0;
            return (
              <div
                key={p.id}
                role="button"
                tabIndex={0}
                onClick={() => onOpen(p.id)}
                onKeyDown={(e) => {
                  if (e.target !== e.currentTarget) return;
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    onOpen(p.id);
                  }
                }}
                className="card group flex h-full cursor-pointer flex-col p-4 text-left transition hover:-translate-y-0.5 hover:shadow-md"
              >
                <div className="flex items-start gap-2">
                  <span className="min-w-0 flex-1 truncate font-semibold text-dh-text">{p.name}</span>
                  {p.running > 0 && (
                    <span className="flex shrink-0 items-center gap-1 text-xs font-medium text-emerald-500">
                      <span className="h-2 w-2 animate-pulse rounded-full bg-emerald-500" />
                      {p.running}
                    </span>
                  )}
                  <button
                    type="button"
                    className="-mr-1 -mt-1 grid h-7 w-7 shrink-0 place-items-center rounded-md text-slate-300 opacity-100 transition hover:bg-rose-500/20 hover:text-rose-400 focus:bg-rose-500/10 focus:text-rose-400 sm:opacity-0 sm:group-hover:opacity-100 sm:focus:opacity-100"
                    title="删除项目"
                    aria-label={`删除项目 ${p.name}`}
                    onClick={(e) => {
                      e.stopPropagation();
                      onDeleteProject(p);
                    }}
                  >
                    🗑
                  </button>
                </div>
                <div className="mt-1 truncate text-[11px] text-slate-400" title={p.path}>
                  {p.path}
                </div>
                {p.autopilot && (
                  <div className="mt-2 text-[11px]">
                    {p.autopilot.status === "running" ? (
                      <span className="flex items-center gap-1 font-medium text-emerald-300">
                        <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-500" />
                        🤖 自愈中 · 第 {p.autopilot.iteration}/{p.autopilot.max_iterations} 轮
                      </span>
                    ) : (
                      <span className="truncate text-slate-400" title={p.autopilot.summary ?? ""}>
                        🤖 上次自愈：{p.autopilot.summary || p.autopilot.status}
                      </span>
                    )}
                  </div>
                )}
                <div
                  className={`mt-1.5 text-[10px] ${
                    p.last_analyze_at && Date.now() / 1000 - p.last_analyze_at > 3 * 86400
                      ? "font-medium text-amber-500"
                      : "text-slate-400"
                  }`}
                >
                  🔍{" "}
                  {p.last_analyze_at ? (
                    <>
                      审查 {ago(p.last_analyze_at)}
                      {Date.now() / 1000 - p.last_analyze_at > 3 * 86400 && " · 该审了"}
                      {p.audit_skipped && <span className="text-emerald-500"> · 未变已跳过</span>}
                    </>
                  ) : (
                    <span className="text-slate-300">未审查</span>
                  )}
                </div>
                <div className="mt-auto flex flex-wrap gap-1.5 pt-3 text-[11px]">
                  <span className="rounded-md bg-dh-s2 px-2 py-0.5 text-dh-tsoft">{p.task_total} 任务</span>
                  {draft > 0 && (
                    <span className="rounded-md bg-slate-400/10 px-2 py-0.5 text-dh-tsoft">{draft} 待办</span>
                  )}
                  {p.manual_open > 0 && (
                    <span className="rounded-md bg-amber-500/10 px-2 py-0.5 text-amber-400">
                      人工 {p.manual_open}
                    </span>
                  )}
                  {p.auto_open > 0 && (
                    <span className="rounded-md bg-violet-500/15 px-2 py-0.5 text-violet-300">
                      自动 {p.auto_open}
                    </span>
                  )}
                  {p.fixed_findings > 0 && (
                    <span className="rounded-md bg-emerald-500/10 px-2 py-0.5 text-emerald-300">
                      已修 {p.fixed_findings}
                    </span>
                  )}
                  {p.policy_count > 0 && (
                    <span className="rounded-md bg-dh-s2 px-2 py-0.5 text-dh-muted" title="已启用的定时自愈策略">
                      🕒 {p.policy_count} 策略
                    </span>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </div>
      )}
    </div>
  );
}

function Stat({ label, value, tone }: { label: string; value: any; tone?: string }) {
  return (
    <div className="px-3 py-2.5 md:px-5 md:py-3.5">
      <div className="text-xs text-slate-400">{label}</div>
      <div className={`text-lg font-semibold md:text-xl ${tone ?? "text-dh-text"}`}>{value}</div>
    </div>
  );
}

function HostMetricsLine({ host }: { host: HostMetrics | null }) {
  const metrics = [
    {
      label: "CPU 温度",
      value: tempMetric(host),
      tone: hostMetricTone("temp", host?.cpu_temp_c),
      title: "宿主机 CPU 温度",
    },
    {
      label: "CPU 负载",
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
      label: "磁盘",
      value: pctMetric(host, "disk_load_pct"),
      tone: hostMetricTone("load", host?.disk_load_pct),
      title: "宿主机磁盘负载",
    },
  ];
  return (
    <span className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 whitespace-nowrap border-dh-bsoft text-slate-400 md:border-l md:pl-3">
      {metrics.map((m) => (
        <span key={m.label} className="flex items-baseline gap-1" title={m.title}>
          <span>{m.label}</span>
          <span className={`tabular-nums font-semibold ${m.tone}`}>{m.value}</span>
        </span>
      ))}
    </span>
  );
}
