// 图表配色（暗色主题下保持高可读度）
export const CHART = {
  mint: "#5ed6a4",
  coral: "#fb7185",
  yellow: "#fbbf24",
  blue: "#60a5fa",
  purple: "#a78bfa",
  cyan: "#22d3ee",
  slate: "#64748b",
};

export const PIE_COLORS = [
  CHART.yellow,
  CHART.coral,
  CHART.blue,
  CHART.cyan,
  CHART.purple,
  CHART.mint,
];

// 状态色板（暗色）
export const STATUS_STYLE: Record<string, { dot: string; text: string; label: string; pulse?: boolean }> = {
  draft: { dot: "bg-slate-400", text: "text-slate-400", label: "待办" },
  queued: { dot: "bg-indigo-400", text: "text-indigo-400", label: "排队中" },
  running: { dot: "bg-emerald-400", text: "text-emerald-400", label: "运行中" },
  waiting_input: { dot: "bg-amber-400", text: "text-amber-400", label: "待输入", pulse: true },
  // 非真实 DB 状态：waiting_input 的细分，用后端 waiting_kind/pending_perm 派生
  waiting_perm: { dot: "bg-fuchsia-400", text: "text-fuchsia-400", label: "待授权", pulse: true },
  waiting_choice: { dot: "bg-orange-400", text: "text-orange-400", label: "待选择", pulse: true },
  waiting_review: { dot: "bg-cyan-400", text: "text-cyan-400", label: "待人工核对", pulse: true },
  rate_limited: { dot: "bg-yellow-500", text: "text-yellow-500", label: "限流等待", pulse: true },
  paused: { dot: "bg-violet-400", text: "text-violet-300", label: "等待执行" },
  done: { dot: "bg-sky-400", text: "text-sky-400", label: "完成" },
  failed: { dot: "bg-rose-400", text: "text-rose-400", label: "失败" },
  cancelled: { dot: "bg-slate-500", text: "text-slate-400", label: "已取消" },
  interrupted: { dot: "bg-slate-500", text: "text-slate-400", label: "中断" },
};

export function taskStatusStyleKey(
  task: { status: string; pending_perm?: boolean; waiting_kind?: string | null },
  hasLivePermission = false,
): string {
  if (task.status !== "waiting_input") return task.status;
  if (hasLivePermission || task.pending_perm || task.waiting_kind === "permission") return "waiting_perm";
  if (task.waiting_kind === "choice") return "waiting_choice";
  if (task.waiting_kind === "review") return "waiting_review";
  return "waiting_input";
}
