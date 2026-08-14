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
import { confirmDialog, promptDialog } from "../overlay";
import { guardQuota } from "../quota";
import type { Task } from "../types";
import { useSingleFlight } from "../useSingleFlight";
import { TaskCard } from "./TaskCard";
import { taskPromptText } from "../taskText";

type ColKey = "backlog" | "active" | "done";
type DoneArchive = { key: string; tasks: Task[] };

const COLUMNS: { key: ColKey; title: string; accent: string; statuses: string[] }[] = [
  { key: "backlog", title: "待办", accent: "bg-slate-400", statuses: ["draft"] },
  { key: "active", title: "执行中", accent: "bg-emerald-500", statuses: ["queued", "running", "waiting_input", "rate_limited"] },
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
  children,
}: {
  col: (typeof COLUMNS)[number];
  tasks: Task[];
  children: ReactNode;
}) {
  const { setNodeRef, isOver } = useDroppable({ id: `col:${col.key}` });
  return (
    <div
      ref={setNodeRef}
      className={`flex min-h-[60vh] w-[82vw] shrink-0 snap-center flex-col rounded-2xl border bg-dh-soft p-3 transition sm:w-[320px] lg:min-w-0 lg:basis-0 lg:flex-1 lg:shrink ${
        isOver ? "border-slate-400/40 bg-slate-400/10" : "border-dh-bsoft"
      }`}
    >
      <div className="mb-3 flex items-center gap-2 px-1">
        <span className={`h-2.5 w-2.5 rounded-full ${col.accent}`} />
        <span className="text-sm font-semibold text-dh-tsoft">{col.title}</span>
        <span className={`rounded-full px-2 text-xs font-medium ring-1 ${countTone(col.key)}`}>
          {tasks.length}
        </span>
      </div>
      <div className="flex max-h-[calc(100dvh-200px)] flex-1 flex-col gap-2 overflow-y-auto pr-0.5">
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

  // 受控子任务在看板上作为父任务的子行渲染：先按父任务归拢，再把「与父任务同列」的
  // 子任务从顶层列表里摘掉（它们由父卡片负责渲染）。父子不同列时（例如父仍在执行、
  // 子已完成归档）子任务照常独立显示，否则会凭空消失。
  const colByTaskId = new Map<number, ColKey>();
  for (const t of visibleTasks) colByTaskId.set(t.id, colOf(t.status));
  const childrenByParent = new Map<number, Task[]>();
  for (const t of visibleTasks) {
    const pid = t.parent_task_id;
    if (pid == null || colByTaskId.get(pid) !== colOf(t.status)) continue;
    const list = childrenByParent.get(pid) ?? [];
    list.push(t);
    childrenByParent.set(pid, list);
  }
  for (const list of childrenByParent.values()) list.sort((a, b) => a.id - b.id);
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
    return (
      <div key={`grp-${t.id}`} className="flex flex-col gap-1.5">
        {card}
        <div className="ml-3 flex flex-col gap-1.5 border-l border-dh-bsoft pl-2.5">
          <div className="text-[11px] text-slate-500">派生的子任务 {kids.length}</div>
          {kids.map((k) => renderSingleTaskCard(k, true))}
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
          await api.continueTask(tk.id, { prompt, start: true });
          onChanged();
        }}
      />
    );
  }

  return (
    <DndContext sensors={sensors} collisionDetection={pointerWithin} onDragEnd={onDragEnd}>
      <div className="dh-scrollbar-none -mx-1 flex snap-x snap-mandatory gap-3 overflow-x-auto px-1 pb-2 lg:mx-0 lg:snap-none lg:overflow-visible lg:px-0">
        {COLUMNS.map((col) => (
          <Column key={col.key} col={col} tasks={grouped[col.key]}>
            <SortableContext
              items={grouped[col.key].map((t) => t.id)}
              strategy={verticalListSortingStrategy}
            >
              {grouped[col.key].length === 0 && (
                <div className="rounded-xl border border-dashed border-dh-bsoft py-8 text-center text-xs text-slate-400">
                  {col.key === "backlog" ? "拖任务到这里 / +任务" : "拖拽到此列"}
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
