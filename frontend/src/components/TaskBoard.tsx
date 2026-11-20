import {
  DndContext,
  PointerSensor,
  pointerWithin,
  useDroppable,
  useSensor,
  useSensors,
  type DragEndEvent,
} from "@dnd-kit/core";
import { SortableContext, arrayMove, verticalListSortingStrategy } from "@dnd-kit/sortable";
import { useEffect, useState, type ReactNode } from "react";
import { api } from "../api";
import { confirmDialog, promptDialog, toast } from "../overlay";
import { guardQuota } from "../quota";
import type { Task } from "../types";
import { STATUS_STYLE, taskStatusStyleKey } from "../theme";
import { useSingleFlight } from "../useSingleFlight";
import { TaskCard } from "./TaskCard";
import { taskPromptText } from "../taskText";

type ColKey = "backlog" | "active" | "done";
type DoneArchive = { key: string; tasks: Task[] };

const COLUMNS: { key: ColKey; title: string; accent: string; statuses: string[] }[] = [
  { key: "backlog", title: "待办", accent: "bg-slate-400", statuses: ["draft"] },
  { key: "active", title: "执行中", accent: "bg-emerald-500", statuses: ["queued", "running", "waiting_input"] },
  { key: "done", title: "已完成", accent: "bg-sky-400", statuses: ["done", "failed", "cancelled", "interrupted"] },
];

function colOf(status: string): ColKey {
  for (const c of COLUMNS) if (c.statuses.includes(status)) return c.key;
  return "done";
}

function countTone(col: ColKey): string {
  if (col === "active") return "bg-emerald-500/10 text-emerald-300 ring-dh-border";
  if (col === "done") return "bg-sky-500/10 text-sky-300 ring-dh-border";
  return "bg-slate-400/10 text-dh-tsoft ring-dh-border";
}

function archiveSeconds(t: Task): number {
  return t.ended_at ?? t.created_at;
}

function dateKeyFromDate(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function archiveDateKey(t: Task): string {
  return dateKeyFromDate(new Date(archiveSeconds(t) * 1000));
}

function byDoneArchive(a: Task, b: Task): number {
  const dayA = archiveDateKey(a);
  const dayB = archiveDateKey(b);
  if (dayA !== dayB) return dayA < dayB ? 1 : -1;
  return b.id - a.id;
}

function archiveDoneTasks(tasks: Task[]): DoneArchive[] {
  const archives: DoneArchive[] = [];
  for (const task of [...tasks].sort(byDoneArchive)) {
    const key = archiveDateKey(task);
    const archive = archives[archives.length - 1];
    if (archive?.key === key) {
      archive.tasks.push(task);
    } else {
      archives.push({ key, tasks: [task] });
    }
  }
  return archives;
}

function Column({
  col,
  tasks,
  phoneVisible,
  children,
}: {
  col: (typeof COLUMNS)[number];
  tasks: Task[];
  /** 手机上一次只显示一列（由顶部分段切换），其余列隐藏 */
  phoneVisible: boolean;
  children: ReactNode;
}) {
  const { setNodeRef, isOver } = useDroppable({ id: `col:${col.key}` });
  return (
    <div
      ref={setNodeRef}
      className={`${phoneVisible ? "flex" : "hidden sm:flex"} w-full shrink-0 snap-center flex-col rounded-2xl border bg-dh-soft p-3 transition sm:min-h-[60vh] sm:w-[320px] lg:min-w-0 lg:basis-0 lg:flex-1 lg:shrink ${
        isOver ? "border-slate-400/40 bg-slate-400/10" : "border-dh-bsoft"
      }`}
    >
      <div className="mb-3 hidden items-center gap-2 px-1 sm:flex">
        <span className={`h-2.5 w-2.5 rounded-full ${col.accent}`} />
        <span className="text-sm font-semibold text-dh-tsoft">{col.title}</span>
        <span className={`rounded-full px-2 text-xs font-medium ring-1 ${countTone(col.key)}`}>
          {tasks.length}
        </span>
      </div>
      {/* 手机上随页面滚动：列内再套一层滚动区，在小屏上很难滑到底 */}
      <div className="flex flex-1 flex-col gap-2 pr-0.5 sm:max-h-[calc(100dvh-200px)] sm:overflow-y-auto">
        {children}
      </div>
    </div>
  );
}

function DoneArchiveGroups({
  archives,
  renderTask,
}: {
  archives: DoneArchive[];
  renderTask: (task: Task) => ReactNode;
}) {
  const todayKey = dateKeyFromDate(new Date());
  const todayArchiveKey = archives.some((archive) => archive.key === todayKey) ? todayKey : null;
  const [openKey, setOpenKey] = useState<string | null>(todayArchiveKey);
  const [showOlder, setShowOlder] = useState(false);

  useEffect(() => {
    if (!todayArchiveKey) return;
    setOpenKey((cur) => cur ?? todayArchiveKey);
  }, [todayArchiveKey]);

  // 默认只显示距最新归档日期 7 天内的分组，更早的点「显示更早」再展示
  let visibleArchives = archives;
  let olderArchives: DoneArchive[] = [];
  if (!showOlder && archives.length > 0) {
    const cutoffMs = Date.parse(archives[0].key) - 6 * 24 * 60 * 60 * 1000;
    visibleArchives = archives.filter((archive) => Date.parse(archive.key) >= cutoffMs);
    olderArchives = archives.slice(visibleArchives.length);
  }
  const olderTaskCount = olderArchives.reduce((count, archive) => count + archive.tasks.length, 0);

  return (
    <>
      {visibleArchives.map((archive) => (
        <details
          key={archive.key}
          open={openKey === archive.key}
          onToggle={(e) => {
            const isOpen = e.currentTarget.open;
            setOpenKey((cur) => (isOpen ? archive.key : cur === archive.key ? null : cur));
          }}
        >
          <summary className="sticky top-0 z-10 flex cursor-pointer select-none items-center gap-2 border-b border-dh-bsoft bg-dh-bg/75 px-1 py-1.5 text-xs font-semibold text-slate-200 backdrop-blur">
            <span>{archive.key}</span>
            <span className="ml-auto rounded-full bg-dh-soft px-1.5 py-0.5 text-[10px] font-medium text-slate-300 ring-1 ring-slate-700">
              {archive.tasks.length}
            </span>
          </summary>
          <div className="mt-2 flex flex-col gap-2">{archive.tasks.map(renderTask)}</div>
        </details>
      ))}
      {olderArchives.length > 0 && (
        <button
          type="button"
          onClick={() => setShowOlder(true)}
          className="w-full rounded-xl border border-dashed border-dh-bsoft bg-dh-soft px-3 py-2 text-center text-xs font-medium text-dh-muted hover:bg-dh-hover hover:text-slate-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-dh-accent"
        >
          显示更早的归档 · {olderArchives.length} 天 / {olderTaskCount} 个任务
        </button>
      )}
    </>
  );
}

export function TaskBoard({
  tasks,
  query = "",
  onOpenTerminal,
  onChanged,
  setTasks,
}: {
  tasks: Task[];
  query?: string;
  onOpenTerminal: (t: Task) => void;
  onChanged: () => void;
  setTasks: (t: Task[]) => void;
}) {
  const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 4 } }));
  const { busy, run } = useSingleFlight();
  const [, setTick] = useState(0);
  // 手机分段切换选中的列；null = 自动（有执行中就看执行中，否则看第一个非空列）
  const [phoneCol, setPhoneCol] = useState<ColKey | null>(null);
  // 默认把子任务收敛挂载到所属主任务下方；排障时可一键平铺显示
  const [flattenSubtasks, setFlattenSubtasks] = useState(false);
  const [expandedParents, setExpandedParents] = useState<Record<number, boolean>>({});

  const normalizedQuery = query.trim().toLocaleLowerCase();
  const boardTasks = tasks.filter((task) => task.status !== "paused");
  const visibleTasks = normalizedQuery
    ? boardTasks.filter((task) => {
        const haystack = `#${task.id}\n${task.id}\n${task.title ?? ""}\n${taskPromptText(task)}`.toLocaleLowerCase();
        return haystack.includes(normalizedQuery);
      })
    : boardTasks;

  // 有活动任务时每秒重渲染，让时长实时跳动
  const hasActive = tasks.some((t) => t.status === "running" || t.status === "waiting_input");
  useEffect(() => {
    if (!hasActive) return;
    const id = setInterval(() => setTick((x) => x + 1), 1000);
    return () => clearInterval(id);
  }, [hasActive]);

  // 只要父任务在当前可见列表中，无论子任务处于哪一列，默认全部收敛挂载到主任务下方；
  // 只有开启「平铺子任务」或父任务不在当前搜索结果里时，子任务才独立显示在看板列中。
  const visibleIds = new Set(visibleTasks.map((t) => t.id));
  const hasAnySubtasks = visibleTasks.some((t) => t.parent_task_id != null && visibleIds.has(t.parent_task_id));
  const childrenByParent = new Map<number, Task[]>();
  if (!flattenSubtasks) {
    for (const t of visibleTasks) {
      const pid = t.parent_task_id;
      if (pid == null || !visibleIds.has(pid)) continue;
      const list = childrenByParent.get(pid) ?? [];
      list.push(t);
      childrenByParent.set(pid, list);
    }
    for (const list of childrenByParent.values()) list.sort((a, b) => a.id - b.id);
  }
  const nestedIds = new Set([...childrenByParent.values()].flat().map((t) => t.id));

  const grouped: Record<ColKey, Task[]> = { backlog: [], active: [], done: [] };
  for (const t of visibleTasks) {
    if (nestedIds.has(t.id)) continue;
    grouped[colOf(t.status)].push(t);
  }
  grouped.backlog.sort((a, b) => b.priority - a.priority);
  const doneArchives = archiveDoneTasks(grouped.done);

  async function reprioritize(next: Task[]) {
    await run(async () => {
      const backlog = next.filter((t) => t.status === "draft");
      const n = backlog.length;
      const withPr = backlog.map((t, i) => ({ ...t, priority: n - i }));
      // 合并回全量 tasks 供乐观更新
      const map = new Map(withPr.map((t) => [t.id, t.priority]));
      setTasks(tasks.map((t) => (map.has(t.id) ? { ...t, priority: map.get(t.id)! } : t)));
      await api.reorder(withPr.map((t) => ({ id: t.id, priority: t.priority })));
      onChanged();
    });
  }

  async function onDragEnd(e: DragEndEvent) {
    if (normalizedQuery) return;
    const { active, over } = e;
    if (!over || busy) return;
    const activeTask = tasks.find((t) => t.id === active.id);
    if (!activeTask) return;

    const overId = String(over.id);
    const targetCol: ColKey = overId.startsWith("col:")
      ? (overId.slice(4) as ColKey)
      : colOf(tasks.find((t) => t.id === over.id)?.status ?? "draft");
    const sourceCol = colOf(activeTask.status);

    // 同为待办列 → 改优先级
    if (sourceCol === "backlog" && targetCol === "backlog" && active.id !== over.id) {
      const ids = grouped.backlog.map((t) => t.id);
      const oldIdx = ids.indexOf(Number(active.id));
      const newIdx = ids.indexOf(Number(over.id));
      if (oldIdx >= 0 && newIdx >= 0) {
        const moved = arrayMove(grouped.backlog, oldIdx, newIdx);
        await reprioritize(moved);
      }
      return;
    }
    if (targetCol === sourceCol) return;

    await run(async () => {
      if (targetCol === "active" && activeTask.status === "draft") {
        if (!(await guardQuota(activeTask.engine))) return;
        await api.startTask(activeTask.id);
      } else if (targetCol === "backlog" && activeTask.status === "queued") {
        await api.toDraft(activeTask.id);
      } else if (targetCol === "done") {
        await api.completeTask(activeTask.id);
      }
      onChanged();
    });
  }

  function renderTaskCard(t: Task) {
    const kids = childrenByParent.get(t.id);
    const card = renderSingleTaskCard(t);
    if (!kids?.length) return card;

    const doneCount = kids.filter((k) => k.status === "done").length;
    const runningCount = kids.filter((k) => k.status === "running" || k.status === "queued").length;
    const waitingCount = kids.filter((k) => k.status === "waiting_input").length;
    const failedCount = kids.filter((k) => k.status === "failed").length;
    const hasUrgentOrLive = runningCount > 0 || waitingCount > 0 || failedCount > 0;
    const isOpen = expandedParents[t.id] ?? hasUrgentOrLive;

    return (
      <div key={`grp-${t.id}`} className="flex flex-col">
        {card}
        <div className="mx-1.5 -mt-1.5 rounded-b-xl border border-t-0 border-dh-bsoft bg-dh-soft/80 px-2.5 pb-2 pt-2.5">
          <button
            type="button"
            onClick={() => setExpandedParents((prev) => ({ ...prev, [t.id]: !isOpen }))}
            className="flex w-full items-center gap-1.5 text-left text-[11px] text-dh-muted hover:text-dh-text"
          >
            <span className="text-[10px]">{isOpen ? "▾" : "▸"}</span>
            <span className="font-medium text-dh-tsoft">
              子任务 {doneCount}/{kids.length}
            </span>
            {runningCount > 0 && (
              <span className="rounded bg-sky-500/15 px-1.5 py-0.5 text-[10px] text-sky-400">
                {runningCount} 运行中
              </span>
            )}
            {waitingCount > 0 && (
              <span className="rounded bg-amber-500/20 px-1.5 py-0.5 text-[10px] font-medium text-amber-400">
                {waitingCount} 待处理
              </span>
            )}
            {failedCount > 0 && (
              <span className="rounded bg-rose-500/20 px-1.5 py-0.5 text-[10px] font-medium text-rose-400">
                {failedCount} 失败
              </span>
            )}
            <span className="ml-auto text-[10px] text-dh-muted">{isOpen ? "收起" : "展开"}</span>
          </button>

          {isOpen && (
            <div className="mt-2 space-y-1">
              {kids.map((k) => {
                const st = STATUS_STYLE[taskStatusStyleKey(k)] ?? STATUS_STYLE.draft;
                return (
                  <button
                    key={k.id}
                    type="button"
                    onClick={() => onOpenTerminal(k)}
                    title={taskPromptText(k)}
                    className="flex w-full items-center gap-1.5 rounded-lg border border-dh-bsoft bg-dh-surface px-2 py-1.5 text-left text-xs transition hover:border-dh-accent"
                  >
                    <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${st.dot} ${st.pulse ? "animate-pulse" : ""}`} />
                    <span className="shrink-0 font-mono text-[10px] text-dh-muted">#{k.id}</span>
                    <span className="shrink-0 rounded bg-dh-s2 px-1 text-[10px] uppercase text-dh-muted">{k.engine}</span>
                    <span className="min-w-0 flex-1 truncate text-[12px] text-dh-text">
                      {k.title || taskPromptText(k)}
                    </span>
                    <span className={`shrink-0 text-[10px] font-medium ${st.text}`}>{st.label}</span>
                    <span className="shrink-0 text-[10px] text-dh-muted">↗</span>
                  </button>
                );
              })}
            </div>
          )}
        </div>
      </div>
    );
  }

  // nested=true 的卡片不在 SortableContext.items 里（items 只含顶层任务），
  // 必须关掉拖拽，否则拖一个未注册的 sortable 会有未定义行为。
  function renderSingleTaskCard(t: Task, nested = false) {
    return (
      <TaskCard
        key={t.id}
        dragDisabled={nested}
        task={t}
        onOpen={onOpenTerminal}
        onStart={async (tk) => {
          if (!(await guardQuota(tk.engine))) return;
          await api.startTask(tk.id);
          onChanged();
        }}
        onComplete={async (tk) => {
          await api.completeTask(tk.id);
          onChanged();
        }}
        onCancel={async (tk) => {
          await api.cancelTask(tk.id);
          onChanged();
        }}
        onDelete={async (tk) => {
          if (await confirmDialog(`删除任务 #${tk.id}「${tk.title || taskPromptText(tk).slice(0, 20)}」？`, { danger: true })) {
            await api.deleteTask(tk.id);
            onChanged();
          }
        }}
        onRerun={async (tk) => {
          if (!(await guardQuota(tk.engine))) return;
          await api.rerunTask(tk.id);
          onChanged();
        }}
        onContinue={async (tk) => {
          const prompt = await promptDialog("追加指令（留空=只加载上下文继续）", "", {
            attachToTaskId: tk.id,
          });
          if (prompt === null) return; // 取消弹窗=不继续；留空=只恢复上下文不发指令
          if (!(await guardQuota(tk.engine))) return;
          try {
            await api.continueTask(tk.id, { prompt, start: true });
          } catch (e) {
            // 如会话正在 Claude 后台运行：后端会说明原因和处理办法，必须让用户看到
            toast(`继续失败：${e instanceof Error ? e.message : String(e)}`, "error");
            return;
          }
          onChanged();
        }}
      />
    );
  }

  const autoCol: ColKey =
    grouped.active.length > 0
      ? "active"
      : (COLUMNS.find((c) => grouped[c.key].length > 0)?.key ?? "active");
  const shownCol = phoneCol ?? autoCol;

  return (
    <DndContext sensors={sensors} collisionDetection={pointerWithin} onDragEnd={onDragEnd}>
      {hasAnySubtasks && (
        <div className="mb-2 flex items-center justify-end">
          <button
            type="button"
            onClick={() => setFlattenSubtasks((v) => !v)}
            className={`rounded-lg border px-2.5 py-1 text-[11px] transition ${
              flattenSubtasks
                ? "border-dh-accent bg-dh-accent/15 text-dh-accent"
                : "border-dh-bsoft bg-dh-surface text-dh-muted hover:text-dh-text"
            }`}
          >
            {flattenSubtasks ? "已平铺子任务（点击收敛到主任务下）" : "子任务已收敛到主任务下 · 平铺查看"}
          </button>
        </div>
      )}
      {/* 手机：分段切换代替横滑看板，避免一进来先看到一整屏空的「待办」列 */}
      <div className="mb-2 grid grid-cols-3 gap-1 rounded-xl border border-dh-bsoft bg-dh-soft p-1 sm:hidden">
        {COLUMNS.map((col) => {
          const on = col.key === shownCol;
          return (
            <button
              key={col.key}
              type="button"
              className={`flex min-h-[36px] items-center justify-center gap-1.5 rounded-lg text-sm ${
                on ? "bg-dh-surface font-medium text-dh-text shadow-sm" : "text-dh-tsoft"
              }`}
              onClick={() => setPhoneCol(col.key)}
            >
              <span className={`h-2 w-2 rounded-full ${col.accent}`} />
              {col.title}
              <span className={`rounded-full px-1.5 text-[11px] font-medium ring-1 ${countTone(col.key)}`}>
                {grouped[col.key].length}
              </span>
            </button>
          );
        })}
      </div>
      <div className="dh-scrollbar-none -mx-1 flex snap-x snap-mandatory gap-3 overflow-x-auto px-1 pb-2 lg:mx-0 lg:snap-none lg:overflow-visible lg:px-0">
        {COLUMNS.map((col) => (
          <Column key={col.key} col={col} tasks={grouped[col.key]} phoneVisible={col.key === shownCol}>
            <SortableContext
              items={grouped[col.key].map((t) => t.id)}
              strategy={verticalListSortingStrategy}
            >
              {grouped[col.key].length === 0 && (
                <div className="rounded-xl border border-dashed border-dh-bsoft py-8 text-center text-xs text-slate-400">
                  <span className="sm:hidden">暂无{col.title}任务</span>
                  <span className="hidden sm:inline">{col.key === "backlog" ? "拖任务到这里 / +任务" : "拖拽到此列"}</span>
                </div>
              )}
              {col.key === "done" ? (
                <DoneArchiveGroups archives={doneArchives} renderTask={renderTaskCard} />
              ) : (
                grouped[col.key].map(renderTaskCard)
              )}
            </SortableContext>
          </Column>
        ))}
      </div>
    </DndContext>
  );
}
