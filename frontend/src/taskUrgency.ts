// 任务紧急度：终端面板的上下切换、快速切换面板都按它排序。
// 纯函数、不依赖 UI，便于 node --test 直接测。

export type UrgencyTask = {
  id: number;
  status: string;
  title?: string | null;
  prompt?: string | null;
  engine?: string;
  project_id?: number;
  waiting_kind?: string | null;
  pending_perm?: boolean;
  waiting_since?: number | null;
  report_summary?: string | null;
  report_result?: string | null;
};

export type UrgencyGroup = "attention" | "running" | "queued" | "review" | "other";

export const URGENCY_GROUP_LABEL: Record<UrgencyGroup, string> = {
  attention: "需要你处理",
  running: "运行中",
  queued: "排队中",
  review: "待核对",
  other: "其他",
};

const GROUP_ORDER: UrgencyGroup[] = ["attention", "running", "queued", "review", "other"];

/**
 * 分组：等你答复/选择/授权的最先；跑完等核对（review）的已不需要盯，排在运行与排队之后，
 * 免得上下切换时总被它们挡在前面。
 */
export function urgencyGroup(task: UrgencyTask): UrgencyGroup {
  if (task.status === "waiting_input") {
    // 回报 needs_input = agent 在等你答复，即便旧后端把等待类型标成 review 也算
    const needsYou = task.pending_perm || task.report_result === "needs_input" || task.waiting_kind !== "review";
    return needsYou ? "attention" : "review";
  }
  if (task.status === "running") return "running";
  if (task.status === "queued") return "queued";
  return "other";
}

/** 按紧急度排序（不改原数组）：组序 → 组内等得越久越靠前 → 创建先后。 */
export function sortByUrgency<T extends UrgencyTask>(tasks: T[]): T[] {
  return tasks
    .map((task, index) => ({ task, index }))
    .sort((a, b) => {
      const ga = GROUP_ORDER.indexOf(urgencyGroup(a.task));
      const gb = GROUP_ORDER.indexOf(urgencyGroup(b.task));
      if (ga !== gb) return ga - gb;
      if (ga === 0) {
        const wa = a.task.waiting_since ?? Number.POSITIVE_INFINITY;
        const wb = b.task.waiting_since ?? Number.POSITIVE_INFINITY;
        if (wa !== wb) return wa - wb;
      }
      return a.index - b.index;
    })
    .map(({ task }) => task);
}

/** 当前任务之外、等得最久的「需要你处理」任务；没有返回 null。 */
export function nextAttentionTask<T extends UrgencyTask>(tasks: T[], currentId: number): T | null {
  return sortByUrgency(tasks).find((t) => t.id !== currentId && urgencyGroup(t) === "attention") ?? null;
}

/** 按搜索词过滤：编号（可带 #）、标题、指令、项目名、引擎、回报摘要，空格分词全部命中才算。 */
export function matchesQuery(task: UrgencyTask, query: string, projectName = ""): boolean {
  const words = query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
  if (words.length === 0) return true;
  const haystack = [
    `#${task.id}`,
    task.title ?? "",
    task.prompt ?? "",
    projectName,
    task.engine ?? "",
    task.report_summary ?? "",
  ]
    .join("\n")
    .toLocaleLowerCase();
  return words.every((word) => haystack.includes(word));
}

/** 过滤并按组聚合，组内保持紧急度顺序；空组不返回。 */
export function groupForSwitcher<T extends UrgencyTask>(
  tasks: T[],
  query: string,
  projectName: (task: T) => string = () => "",
): { group: UrgencyGroup; tasks: T[] }[] {
  const sorted = sortByUrgency(tasks).filter((t) => matchesQuery(t, query, projectName(t)));
  return GROUP_ORDER.map((group) => ({ group, tasks: sorted.filter((t) => urgencyGroup(t) === group) })).filter(
    (g) => g.tasks.length > 0,
  );
}
