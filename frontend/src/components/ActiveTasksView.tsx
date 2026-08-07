import {
  DndContext,
  PointerSensor,
  pointerWithin,
  useDraggable,
  useDroppable,
  useSensor,
  useSensors,
  type DragEndEvent,
} from "@dnd-kit/core";
import { CSS } from "@dnd-kit/utilities";
import { useCallback, useEffect, useMemo, useRef, useState, type MouseEvent, type ReactNode } from "react";
import { api } from "../api";
import { confirmDialog, promptDialog, toast } from "../overlay";
import { guardQuota } from "../quota";
import { STATUS_STYLE, taskStatusStyleKey } from "../theme";
import type { Project, Task } from "../types";
import { CreateTaskDialog } from "./CreateTaskDialog";
import { TerminalPanel } from "./TerminalPanel";
import { TerminalWorkspace, type WorkspaceLayout } from "./TerminalWorkspace";
import { taskPromptText } from "../taskText";
import { engineBadgeClass, engineName, engineShort } from "../engines";

const ACTIVE_STATUSES = new Set(["queued", "running", "waiting_input"]);
const ARCHIVED_STATUSES = new Set(["done", "failed", "cancelled", "interrupted"]);

// 任务行主操作（继续执行）的统一按钮样式：等待执行区与归档区共用，跟随全站品牌主色
const RESUME_BTN_CLS =
  "rounded-md bg-dh-accent px-2.5 py-1.5 text-dh-accfg hover:bg-dh-acchov disabled:opacity-50";

function isActiveTask(t: Task): boolean {
  return ACTIVE_STATUSES.has(t.status);
}

function statusKey(t: Task): string {
  return taskStatusStyleKey(t);
}

function attentionLabel(t: Task): string {
  return STATUS_STYLE[statusKey(t)]?.label ?? "待输入";
}

function EngineBadge({ engine }: { engine: Task["engine"] }) {
  return (
    <span
      className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] font-semibold ${engineBadgeClass(engine)}`}
      title={engineName(engine)}
    >
      {engineShort(engine)}
    </span>
  );
}

function EditableTaskTitle({
  task,
  fallback,
  onSaved,
}: {
  task: Task;
  fallback: string;
  onSaved: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState("");
  const savingRef = useRef(false);

  function begin(event: MouseEvent) {
    event.stopPropagation();
    setValue(task.title || "");
    setEditing(true);
  }

  async function commit() {
    if (savingRef.current) return;
    const next = value.trim();
    if (next === (task.title || "")) {
      setEditing(false);
      return;
    }
    savingRef.current = true;
    try {
      await api.updateTask(task.id, { title: next });
      setEditing(false);
      onSaved();
      toast(`任务 #${task.id} 标题已更新`, "success");
    } catch (error) {
      toast(`重命名失败：${error instanceof Error ? error.message : String(error)}`, "error");
    } finally {
      savingRef.current = false;
    }
  }

  if (editing) {
    return (
      <input
        autoFocus
        value={value}
        onChange={(event) => setValue(event.target.value)}
        onClick={(event) => event.stopPropagation()}
        onKeyDown={(event) => {
          event.stopPropagation();
          if (event.key === "Enter") {
            event.preventDefault();
            void commit();
          } else if (event.key === "Escape") {
            event.preventDefault();
            setEditing(false);
          }
        }}
        onBlur={() => void commit()}
        placeholder="任务标题"
        aria-label={`重命名任务 #${task.id}`}
        className="min-w-0 flex-1 rounded border border-dh-accent bg-dh-surface px-1.5 py-0.5 text-sm font-medium text-dh-text outline-none focus:ring-1 focus:ring-dh-accent"
      />
    );
  }

  return (
    <>
      <span className="min-w-0 truncate text-sm font-medium text-dh-text">
        {task.title || fallback}
      </span>
      <button
        type="button"
        className="shrink-0 rounded p-0.5 text-slate-300 hover:bg-dh-hover hover:text-dh-muted focus:outline-none focus-visible:ring-2 focus-visible:ring-dh-accent"
        title="重命名标题"
        aria-label={`重命名任务 #${task.id}`}
        onClick={begin}
      >
        ✎
      </button>
    </>
  );
}

function DraggableTaskRow({ task, children }: { task: Task; children: ReactNode }) {
  const { attributes, listeners, setNodeRef, transform, isDragging } = useDraggable({
    id: `task:${task.id}`,
  });
  return (
    <div
      ref={setNodeRef}
      style={{ transform: CSS.Translate.toString(transform) }}
      className={`relative ${isDragging ? "z-20 opacity-60" : ""}`}
    >
      <button
        type="button"
        {...attributes}
        {...listeners}
        className="absolute left-2 top-1/2 z-10 -translate-y-1/2 cursor-grab rounded p-1 text-slate-300 hover:bg-dh-hover hover:text-dh-muted focus:outline-none focus-visible:ring-2 focus-visible:ring-dh-accent active:cursor-grabbing"
        title="拖到等待执行"
        aria-label={`拖动任务 #${task.id}`}
        onClick={(event) => event.stopPropagation()}
      >
        ⠿
      </button>
      <div className="pl-7">{children}</div>
    </div>
  );
}

function PausedDropZone({ children }: { children: ReactNode }) {
  const { setNodeRef, isOver } = useDroppable({ id: "paused-drop-zone" });
  return (
    <section
      ref={setNodeRef}
      className={`space-y-3 border-t pt-5 transition-colors ${
        isOver ? "border-violet-400 bg-violet-500/10" : "border-dh-bsoft"
      }`}
    >
      {children}
    </section>
  );
}

function fmtDur(start: number | null, end: number | null, accum = 0): string {
  const cur = start ? Math.max(0, Math.round((end ?? Date.now() / 1000) - start)) : 0;
  const secs = cur + (accum || 0);
  if (!start && !accum) return "—";
  if (secs < 60) return `${secs}s`;
  const m = Math.floor(secs / 60);
  return m < 60 ? `${m}m${secs % 60}s` : `${Math.floor(m / 60)}h${m % 60}m`;
}

// 移动端任务卡片：窄屏放不下桌面端的多列表格，改成状态/项目一行、标题一行、
// 时长·Token 带图标、操作独立一行右对齐的紧凑卡片。桌面端(md+)仍走原网格。
function MobileTaskCard({
  task,
  projectName,
  projectPath,
  dotClass,
  pulse = false,
  statusLabel,
  statusTextClass,
  titleFallback,
  subtitle,
  onOpen,
  onReload,
  actions,
}: {
  task: Task;
  projectName: string;
  projectPath?: string;
  dotClass: string;
  pulse?: boolean;
  statusLabel: string;
  statusTextClass: string;
  titleFallback: string;
  subtitle: string | null;
  onOpen: () => void;
  onReload: () => void;
  actions: ReactNode;
}) {
  return (
    <div
      role="button"
      tabIndex={0}
      className="flex flex-col gap-1.5 px-3 py-3 text-left hover:bg-dh-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-dh-accent md:hidden"
      onClick={onOpen}
      onKeyDown={(event) => {
        if (event.target !== event.currentTarget || (event.key !== "Enter" && event.key !== " ")) return;
        event.preventDefault();
        onOpen();
      }}
    >
      <div className="flex items-center gap-2">
        <span className={`h-2.5 w-2.5 shrink-0 rounded-full ${dotClass} ${pulse ? "animate-pulse" : ""}`} />
        <span className={`shrink-0 text-xs font-medium ${statusTextClass}`}>{statusLabel}</span>
        <span className="ml-auto min-w-0 truncate text-xs text-slate-400" title={projectPath}>
          {projectName}
        </span>
      </div>
      <div className="flex min-w-0 items-center gap-2">
        <span className="shrink-0 font-mono text-xs text-slate-400">#{task.id}</span>
        <EngineBadge engine={task.engine} />
        <EditableTaskTitle task={task} fallback={titleFallback} onSaved={onReload} />
      </div>
      {subtitle && <div className="truncate text-xs text-slate-400">{subtitle}</div>}
      <div className="mt-0.5 flex items-center gap-4 text-xs tabular-nums text-slate-400">
        <span className="inline-flex items-center gap-1">
          <span aria-hidden>⏱</span>
          {fmtDur(task.started_at, task.ended_at, task.elapsed_accum)}
        </span>
        <span className="inline-flex items-center gap-1">
          <span aria-hidden>◈</span>
          {task.tokens != null ? task.tokens.toLocaleString() : "—"}
        </span>
      </div>
      <div
        className="flex items-center justify-end gap-1.5 text-xs font-medium"
        onClick={(event) => event.stopPropagation()}
      >
        {actions}
      </div>
    </div>
  );
}

function byCreationOrder(a: Task, b: Task): number {
  return a.created_at - b.created_at || a.id - b.id;
}

function archiveTimestamp(task: Task): number {
  return task.ended_at ?? task.started_at ?? task.created_at;
}

// 打开 N 个终端时，选能容纳的最小布局：1→单格 2→左右 3~4→四宫格
function fitLayout(count: number): WorkspaceLayout {
  return count <= 1 ? 1 : count <= 2 ? 2 : 4;
}

// 终端工作台：一组并排终端（布局 + 任务集合），可在「运行任务」页内以子 tab 切换
type Workspace = {
  id: string;
  name: string;
  layout: WorkspaceLayout;
  taskIds: number[];
};

const WORKSPACES_LS_KEY = "bosun.workspaces.v1";
const LEGACY_WORKSPACES_LS_KEY = "deckhand.workspaces.v1";

function loadWorkspaces(): Workspace[] {
  try {
    let raw = localStorage.getItem(WORKSPACES_LS_KEY);
    if (!raw) {
      // 升级兼容：迁移旧品牌 key 下保存的工作台布局
      const legacy = localStorage.getItem(LEGACY_WORKSPACES_LS_KEY);
      if (legacy) {
        localStorage.setItem(WORKSPACES_LS_KEY, legacy);
        localStorage.removeItem(LEGACY_WORKSPACES_LS_KEY);
        raw = legacy;
      }
    }
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter((w) => w && typeof w.id === "string" && Array.isArray(w.taskIds))
      .map((w) => ({
        id: w.id,
        name: typeof w.name === "string" ? w.name : "工作台",
        layout: ([1, 2, 4] as WorkspaceLayout[]).includes(w.layout) ? w.layout : 1,
        taskIds: w.taskIds.filter((x: unknown) => typeof x === "number").slice(-4),
      }));
  } catch {
    return [];
  }
}

function nextWorkspaceName(list: Workspace[]): string {
  const nums = list.map((w) => {
    const m = /工作台\s*(\d+)/.exec(w.name);
    return m ? Number(m[1]) : 0;
  });
  return `工作台 ${Math.max(0, ...nums) + 1}`;
}

function newWorkspaceId(): string {
  try {
    if (typeof crypto !== "undefined" && crypto.randomUUID) return crypto.randomUUID();
  } catch {
    /* ignore */
  }
  return `ws-${performance.now().toString(36)}-${Math.round(performance.now() % 1000)}`;
}

function archiveDateKey(task: Task): string {
  const date = new Date(archiveTimestamp(task) * 1000);
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

// 「运行任务」页内的子 tab 栏：运行任务(纯列表) + 各工作台 + 新建
function WorkspaceTabs({
  workspaces,
  activeId,
  onSelectList,
  onSelect,
  onNew,
  onClose,
  onRename,
  runningTotal,
  maxConcurrent,
  hasPendingUpdates,
  refreshing,
}: {
  workspaces: Workspace[];
  activeId: string | null;
  onSelectList: () => void;
  onSelect: (id: string) => void;
  onNew: () => void;
  onClose: (id: string) => void;
  onRename: (id: string) => void;
  runningTotal: number;
  maxConcurrent: number;
  hasPendingUpdates: boolean;
  refreshing: boolean;
}) {
  return (
    <div className="dh-scrollbar-none flex shrink-0 items-center gap-1 overflow-x-auto border-b border-dh-bsoft bg-dh-surface px-3 py-2">
      <button
        type="button"
        className={`shrink-0 whitespace-nowrap rounded-lg px-3 py-1.5 text-sm font-medium ${
          activeId === null ? "bg-dh-soft text-white" : "text-dh-tsoft hover:bg-dh-hover"
        }`}
        onClick={onSelectList}
      >
        运行任务
      </button>
      {workspaces.map((w) => (
        <div
          key={w.id}
          className={`flex shrink-0 items-center gap-1 rounded-lg py-1.5 pl-3 pr-1 text-sm ${
            activeId === w.id ? "bg-dh-accent text-dh-accfg" : "text-dh-tsoft hover:bg-dh-hover"
          }`}
        >
          <button
            type="button"
            className="whitespace-nowrap"
            onClick={() => onSelect(w.id)}
            onDoubleClick={() => onRename(w.id)}
            title="点击切换 · 双击改名"
          >
            {w.name}
            {w.taskIds.length > 0 && (
              <span className={`ml-1.5 text-xs ${activeId === w.id ? "text-dh-accfg" : "text-slate-400"}`}>
                {w.taskIds.length}
              </span>
            )}
          </button>
          <button
            type="button"
            className={`rounded p-0.5 text-xs ${
              activeId === w.id ? "text-dh-accfg hover:bg-dh-acchov" : "text-slate-400 hover:bg-dh-hover"
            }`}
            onClick={() => onClose(w.id)}
            title="关闭工作台"
          >
            ✕
          </button>
        </div>
      ))}
      <button
        type="button"
        className="shrink-0 rounded-lg px-2.5 py-1.5 text-sm text-dh-muted hover:bg-dh-hover"
        onClick={onNew}
        title="新建工作台"
      >
        ＋
      </button>
      <div className="ml-auto flex shrink-0 items-center gap-x-3 pl-3 text-xs text-dh-muted">
        <span className="text-emerald-300">
          正在运行{" "}
          <span className="font-semibold tabular-nums text-emerald-300">
            {runningTotal}/{maxConcurrent}
          </span>
        </span>
        {hasPendingUpdates && (
          <span
            className="rounded-md border border-amber-500/40 bg-amber-500/10 px-2 py-0.5 font-medium text-amber-400"
            title="后台事件已到达，下拉刷新列表"
          >
            {refreshing ? "刷新中…" : "有更新"}
          </span>
        )}
      </div>
    </div>
  );
}

export function ActiveTasksView({
  projects,
  reloadKey,
  onDataChanged,
  runningTotal,
  maxConcurrent,
  hasPendingUpdates,
  refreshing,
}: {
  projects: Project[];
  reloadKey: number;
  onDataChanged: () => void;
  runningTotal: number;
  maxConcurrent: number;
  hasPendingUpdates: boolean;
  refreshing: boolean;
}) {
  const [tasks, setTasks] = useState<Task[]>([]);
  // 列表里点单个任务：单终端抽屉（不进工作台）
  const [openTaskId, setOpenTaskId] = useState<number | null>(null);
  // 多个终端工作台 + 当前激活的（null = 显示「运行任务」纯列表）
  const [workspaces, setWorkspaces] = useState<Workspace[]>(loadWorkspaces);
  const [activeWorkspaceId, setActiveWorkspaceId] = useState<string | null>(null);
  const activeWorkspace = workspaces.find((w) => w.id === activeWorkspaceId) ?? null;

  // 工作台变更即持久化到 localStorage（刷新后保留）
  useEffect(() => {
    try {
      localStorage.setItem(WORKSPACES_LS_KEY, JSON.stringify(workspaces));
    } catch {
      /* 隐私模式等写入失败，忽略 */
    }
  }, [workspaces]);

  // 工作台里的任务一旦结束（完成/失败/取消/中断）自动移出格子，释放槽位
  useEffect(() => {
    setWorkspaces((prev) => {
      let mutated = false;
      const next = prev.map((w) => {
        const kept = w.taskIds.filter((id) => {
          const t = tasks.find((x) => x.id === id);
          return !t || !ARCHIVED_STATUSES.has(t.status);
        });
        if (kept.length === w.taskIds.length) return w;
        mutated = true;
        return { ...w, taskIds: kept };
      });
      return mutated ? next : prev;
    });
  }, [tasks]);

  // 点任务：加入「当前激活工作台」；在列表 tab 时落到最近一个工作台，没有则新建一个
  function openTask(id: number) {
    let list = workspaces;
    let targetId =
      activeWorkspaceId && list.some((w) => w.id === activeWorkspaceId) ? activeWorkspaceId : null;
    if (!targetId) {
      if (list.length === 0) {
        list = [...list, { id: newWorkspaceId(), name: nextWorkspaceName(list), layout: 1, taskIds: [] }];
      }
      targetId = list[list.length - 1].id;
    }
    list = list.map((w) => {
      if (w.id !== targetId || w.taskIds.includes(id)) return w;
      const next = [...w.taskIds, id].slice(-4);
      return { ...w, taskIds: next, layout: w.layout >= next.length ? w.layout : fitLayout(next.length) };
    });
    setWorkspaces(list);
    setActiveWorkspaceId(targetId);
  }
  function closeTask(id: number) {
    if (!activeWorkspace) return;
    setWorkspaces((prev) =>
      prev.map((w) => (w.id === activeWorkspace.id ? { ...w, taskIds: w.taskIds.filter((x) => x !== id) } : w)),
    );
  }
  // 手动切小布局时，回收超出的格子（保留最近打开的几个）
  function changeLayout(l: WorkspaceLayout) {
    if (!activeWorkspace) return;
    setWorkspaces((prev) =>
      prev.map((w) =>
        w.id === activeWorkspace.id
          ? { ...w, layout: l, taskIds: w.taskIds.length > l ? w.taskIds.slice(-l) : w.taskIds }
          : w,
      ),
    );
  }
  function createWorkspace() {
    const ws: Workspace = { id: newWorkspaceId(), name: nextWorkspaceName(workspaces), layout: 1, taskIds: [] };
    setWorkspaces((prev) => [...prev, ws]);
    setActiveWorkspaceId(ws.id);
  }
  function closeWorkspace(id: string) {
    setWorkspaces((prev) => prev.filter((w) => w.id !== id));
    if (activeWorkspaceId === id) setActiveWorkspaceId(null);
  }
  async function renameWorkspace(id: string) {
    const ws = workspaces.find((w) => w.id === id);
    if (!ws) return;
    const name = (await promptDialog("工作台名称", ws.name))?.trim();
    if (!name) return;
    setWorkspaces((prev) => prev.map((w) => (w.id === id ? { ...w, name } : w)));
  }
  const [showCreate, setShowCreate] = useState(false);
  const [archiveQuery, setArchiveQuery] = useState("");
  const [archiveDateOverrides, setArchiveDateOverrides] = useState<Record<string, boolean>>({});
  const [showOlderArchives, setShowOlderArchives] = useState(false);
  const [archiveProjectId, setArchiveProjectId] = useState("");
  const [completingTaskId, setCompletingTaskId] = useState<number | null>(null);
  const [transitioningTaskId, setTransitioningTaskId] = useState<number | null>(null);
  const [, setTick] = useState(0);
  const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 4 } }));

  const projectById = useMemo(() => new Map(projects.map((p) => [p.id, p])), [projects]);

  const load = useCallback(async () => {
    setTasks(await api.tasks());
  }, []);

  useEffect(() => {
    load();
  }, [load, reloadKey]);

  const activeTasks = useMemo(
    () => tasks.filter(isActiveTask).sort(byCreationOrder),
    [tasks],
  );
  const pausedTasks = useMemo(
    () => tasks.filter((task) => task.status === "paused").sort((a, b) => b.id - a.id),
    [tasks],
  );
  const archivedTasks = useMemo(() => tasks
    .filter((task) => ARCHIVED_STATUSES.has(task.status))
    .sort((a, b) => archiveTimestamp(b) - archiveTimestamp(a) || b.id - a.id), [tasks]);
  const archivedProjects = useMemo(() => {
    const projectIds = new Set(archivedTasks.map((task) => task.project_id));
    return projects
      .filter((project) => projectIds.has(project.id))
      .sort((a, b) => a.name.localeCompare(b.name, "zh-CN"));
  }, [archivedTasks, projects]);
  const archivedGroups = useMemo(() => {
    const query = archiveQuery.trim().toLocaleLowerCase();
    const archived = archivedTasks.filter((task) => {
      if (archiveProjectId && String(task.project_id) !== archiveProjectId) return false;
      if (!query) return true;
      const project = projectById.get(task.project_id);
      return `#${task.id}\n${task.id}\n${task.engine}\n${task.title ?? ""}\n${taskPromptText(task)}\n${project?.name ?? ""}\n${project?.path ?? ""}`
        .toLocaleLowerCase()
        .includes(query);
    });
    const groups: { date: string; tasks: Task[] }[] = [];
    for (const task of archived) {
      const date = archiveDateKey(task);
      const latest = groups[groups.length - 1];
      if (latest?.date === date) latest.tasks.push(task);
      else groups.push({ date, tasks: [task] });
    }
    return groups;
  }, [archiveProjectId, archiveQuery, archivedTasks, projectById]);
  const archivedCount = archivedGroups.reduce((count, group) => count + group.tasks.length, 0);
  // 默认只展开最新日期；筛选/搜索时全部展开，用户手动点过的日期以其选择为准
  const archiveFiltering = Boolean(archiveQuery.trim() || archiveProjectId);
  // 默认只显示距最新归档日期 7 天内的分组，更早的点「显示更早」再展示；筛选/搜索时不设限
  const { visibleArchivedGroups, olderGroups } = useMemo(() => {
    if (archiveFiltering || showOlderArchives || archivedGroups.length === 0) {
      return { visibleArchivedGroups: archivedGroups, olderGroups: [] as typeof archivedGroups };
    }
    const latestMs = Date.parse(archivedGroups[0].date);
    const cutoffMs = latestMs - 6 * 24 * 60 * 60 * 1000;
    const visible = archivedGroups.filter((group) => Date.parse(group.date) >= cutoffMs);
    return { visibleArchivedGroups: visible, olderGroups: archivedGroups.slice(visible.length) };
  }, [archiveFiltering, archivedGroups, showOlderArchives]);
  const olderTaskCount = olderGroups.reduce((count, group) => count + group.tasks.length, 0);
  const isArchiveDateOpen = (date: string, index: number) =>
    archiveDateOverrides[date] ?? (archiveFiltering || index === 0);
  const toggleArchiveDate = (date: string, index: number) =>
    setArchiveDateOverrides((prev) => ({ ...prev, [date]: !isArchiveDateOpen(date, index) }));
  const totalArchivedCount = archivedTasks.length;
  const attentionTasks = activeTasks.filter((t) => t.status === "waiting_input");
  // 当前工作台已打开的任务（用于面板内切换/可加入列表）
  const activeTaskIds = activeWorkspace?.taskIds ?? [];
  const openTasks = activeTaskIds
    .map((id) => tasks.find((t) => t.id === id))
    .filter((t): t is Task => !!t);
  const switchTasks = [
    ...activeTasks,
    ...openTasks.filter((t) => !activeTasks.some((a) => a.id === t.id)),
  ];
  const queued = activeTasks.filter((t) => t.status === "queued").length;
  const running = activeTasks.filter((t) => t.status === "running").length;
  const review = activeTasks.filter((t) => statusKey(t) === "waiting_review").length;
  const needsInput = activeTasks.filter((t) => {
    const key = statusKey(t);
    return key === "waiting_input" || key === "waiting_choice" || key === "waiting_perm";
  }).length;

  useEffect(() => {
    if (activeTasks.length === 0) return;
    const id = setInterval(() => setTick((x) => x + 1), 1000);
    return () => clearInterval(id);
  }, [activeTasks.length]);

  const changed = () => {
    load();
    onDataChanged();
  };

  async function completeTask(task: Task) {
    if (completingTaskId !== null) return;
    setCompletingTaskId(task.id);
    try {
      await api.completeTask(task.id);
      await load();
      onDataChanged();
      toast(`任务 #${task.id} 已完成`, "success");
    } catch (error) {
      toast(`完成任务失败：${error instanceof Error ? error.message : String(error)}`, "error");
    } finally {
      setCompletingTaskId(null);
    }
  }

  async function pauseTask(task: Task) {
    if (transitioningTaskId !== null) return;
    if (task.status === "running" || task.status === "waiting_input") {
      const confirmed = await confirmDialog(
        `将停止任务 #${task.id}「${task.title || taskPromptText(task).slice(0, 24)}」并移入等待执行，是否继续？`,
        { danger: true },
      );
      if (!confirmed) return;
    }
    setTransitioningTaskId(task.id);
    try {
      await api.pauseTask(task.id);
      await load();
      onDataChanged();
      toast(`任务 #${task.id} 已移入等待执行`, "success");
    } catch (error) {
      await load();
      toast(`暂停任务失败：${error instanceof Error ? error.message : String(error)}`, "error");
    } finally {
      setTransitioningTaskId(null);
    }
  }

  async function resumePausedTask(task: Task) {
    if (transitioningTaskId !== null) return;
    const prompt = await promptDialog("追加指令（留空=只加载上下文继续）", "", {
      attachToTaskId: task.id,
    });
    if (prompt === null) return; // 取消弹窗=不继续
    if (!(await guardQuota(task.engine))) return;
    setTransitioningTaskId(task.id);
    try {
      await api.resumePausedTask(task.id, prompt);
      await load();
      onDataChanged();
      toast(`任务 #${task.id} 已加入执行队列`, "success");
    } catch (error) {
      await load();
      toast(`继续任务失败：${error instanceof Error ? error.message : String(error)}`, "error");
    } finally {
      setTransitioningTaskId(null);
    }
  }

  // 归档任务原地继续：走 /continue 恢复原会话（同一张卡拉回执行中），流程与等待执行的继续一致
  async function continueArchivedTask(task: Task) {
    if (transitioningTaskId !== null) return;
    const prompt = await promptDialog("追加指令（留空=只加载上下文继续）", "", {
      attachToTaskId: task.id,
    });
    if (prompt === null) return; // 取消弹窗=不继续
    if (!(await guardQuota(task.engine))) return;
    setTransitioningTaskId(task.id);
    try {
      await api.continueTask(task.id, { prompt, start: true });
      await load();
      onDataChanged();
      toast(`任务 #${task.id} 已加入执行队列`, "success");
    } catch (error) {
      await load();
      toast(`继续任务失败：${error instanceof Error ? error.message : String(error)}`, "error");
    } finally {
      setTransitioningTaskId(null);
    }
  }


  async function restorePausedTask(task: Task) {
    if (transitioningTaskId !== null) return;
    setTransitioningTaskId(task.id);
    try {
      await api.restorePausedTask(task.id);
      await load();
      onDataChanged();
      toast(`任务 #${task.id} 已移回${ARCHIVED_STATUSES.has(task.paused_from_status ?? "") ? "归档" : "待办"}`, "success");
    } catch (error) {
      await load();
      toast(`移回任务失败：${error instanceof Error ? error.message : String(error)}`, "error");
    } finally {
      setTransitioningTaskId(null);
    }
  }

  async function handleDragEnd(event: DragEndEvent) {
    if (event.over?.id !== "paused-drop-zone") return;
    const id = Number(String(event.active.id).replace("task:", ""));
    const task = tasks.find((item) => item.id === id);
    if (!task || (!ACTIVE_STATUSES.has(task.status) && !ARCHIVED_STATUSES.has(task.status))) return;
    await pauseTask(task);
  }

  return (
    <DndContext
      sensors={sensors}
      collisionDetection={pointerWithin}
      onDragEnd={(event) => void handleDragEnd(event)}
    >
    <div className="flex h-full min-h-0 flex-col">
      <WorkspaceTabs
        workspaces={workspaces}
        activeId={activeWorkspaceId}
        onSelectList={() => setActiveWorkspaceId(null)}
        onSelect={setActiveWorkspaceId}
        onNew={createWorkspace}
        onClose={closeWorkspace}
        onRename={renameWorkspace}
        runningTotal={runningTotal}
        maxConcurrent={maxConcurrent}
        hasPendingUpdates={hasPendingUpdates}
        refreshing={refreshing}
      />
      <div className={activeWorkspace ? "hidden" : "min-h-0 flex-1 overflow-auto"}>
        <div className="space-y-5 px-4 py-5 md:px-8 md:py-6">
      <div className="flex flex-wrap items-center gap-2 border-b border-dh-bsoft pb-4">
        <div className="min-w-0">
          <div className="text-sm font-semibold text-dh-text">运行任务</div>
          <div className="mt-1 text-xs text-slate-400">
            {activeTasks.length} 个活动任务 · {running} 运行 · {review} 待核对 · {needsInput} 待介入 · {queued} 排队
          </div>
        </div>
        <button
          className="ml-auto rounded-lg bg-dh-accent px-3 py-1.5 text-sm font-medium text-dh-accfg shadow-sm shadow-black/30 hover:bg-dh-acchov disabled:opacity-50"
          onClick={() => setShowCreate(true)}
          disabled={projects.length === 0}
          title={projects.length === 0 ? "请先添加项目" : "新建任务"}
        >
          + 新建任务
        </button>
        <button
          className="rounded-lg border border-dh-bsoft bg-dh-surface px-3 py-1.5 text-sm text-dh-tsoft hover:bg-dh-hover"
          onClick={changed}
        >
          ↻ 刷新
        </button>
      </div>

      {attentionTasks.length > 0 && (
        <div className="rounded-xl border border-amber-500/40 bg-amber-500/10 px-3 py-3">
          <div className="mb-2 flex items-center gap-2 text-sm font-semibold text-amber-400">
            <span className="h-2 w-2 animate-pulse rounded-full bg-amber-500" />
            {attentionTasks.length} 个任务需要处理
          </div>
          <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
            {attentionTasks.map((t) => {
              const p = projectById.get(t.project_id);
              const s = STATUS_STYLE[statusKey(t)] ?? STATUS_STYLE.waiting_input;
              return (
                <button
                  key={t.id}
                  className="min-w-0 rounded-lg border border-amber-500/40 bg-dh-surface px-3 py-2 text-left text-sm hover:border-amber-500/40 hover:bg-amber-500/20"
                  onClick={() => setOpenTaskId(t.id)}
                >
                  <div className="flex items-center gap-2">
                    <span className="font-mono text-xs text-slate-400">#{t.id}</span>
                    <EngineBadge engine={t.engine} />
                    <span className={`rounded bg-dh-s2 px-1.5 text-[10px] font-medium ${s.text}`}>
                      {attentionLabel(t)}
                    </span>
                    <span className="min-w-0 truncate text-xs text-slate-400">{p?.name ?? `项目 ${t.project_id}`}</span>
                  </div>
                  <div className="mt-1 truncate font-medium text-dh-text">{t.title || taskPromptText(t)}</div>
                  {t.report_summary && (
                    // agent 回报的处理摘要：待人工处理时先看结论，不用点进终端翻日志
                    <div className="mt-1 line-clamp-2 text-xs text-dh-muted" title={t.report_summary}>
                      {t.report_summary}
                    </div>
                  )}
                </button>
              );
            })}
          </div>
        </div>
      )}

      {activeTasks.length === 0 ? (
          <div className={`flex flex-col items-center justify-center rounded-xl border border-dashed border-dh-bsoft bg-dh-soft text-center ${totalArchivedCount > 0 || pausedTasks.length > 0 ? "min-h-32" : "min-h-[45vh]"}`}>
          <div className="text-sm font-medium text-dh-tsoft">当前没有运行中的任务</div>
          <div className="mt-1 text-xs text-slate-400">从项目看板创建任务并执行后，会出现在这里。</div>
        </div>
      ) : (
        <div className="overflow-hidden rounded-xl border border-dh-bsoft bg-dh-surface">
          <div className="hidden grid-cols-[112px_120px_minmax(0,1fr)_96px_92px_156px] gap-3 border-b border-dh-bsoft bg-dh-soft px-3 py-2 text-xs font-medium text-slate-400 md:grid">
            <span>状态</span>
            <span>项目</span>
            <span>任务</span>
            <span>时长</span>
            <span>Token</span>
            <span className="text-right">操作</span>
          </div>
          <div className="divide-y divide-dh-bsoft">
            {activeTasks.map((t) => {
              const p = projectById.get(t.project_id);
              const s = STATUS_STYLE[statusKey(t)] ?? STATUS_STYLE.draft;
              const actions = (
                <>
                  <span className="text-dh-tsoft">终端</span>
                  <button
                    type="button"
                    className="rounded-md px-1.5 py-1 text-[11px] font-normal text-violet-300 hover:bg-violet-500/20 hover:text-violet-300 disabled:opacity-50"
                    disabled={transitioningTaskId !== null}
                    onClick={(event) => {
                      event.stopPropagation();
                      void pauseTask(t);
                    }}
                  >
                    等待
                  </button>
                  <button
                    type="button"
                    className="group/complete inline-flex items-center gap-1 rounded-md px-1.5 py-1 text-[11px] font-normal text-slate-400 transition-colors hover:bg-emerald-500/20 hover:text-emerald-300 disabled:cursor-wait disabled:opacity-50"
                    disabled={completingTaskId !== null}
                    title="直接标记为完成"
                    onClick={(event) => {
                      event.stopPropagation();
                      void completeTask(t);
                    }}
                  >
                    {completingTaskId === t.id ? (
                      "处理中…"
                    ) : (
                      <>
                        <span className="grid h-4 w-4 place-items-center rounded-full border border-dh-bsoft text-[10px] leading-none transition-colors group-hover/complete:border-emerald-500/40">
                          ✓
                        </span>
                        <span>完成</span>
                      </>
                    )}
                  </button>
                </>
              );
              return (
                <DraggableTaskRow key={t.id} task={t}>
                <MobileTaskCard
                  task={t}
                  projectName={p?.name ?? `项目 ${t.project_id}`}
                  projectPath={p?.path}
                  dotClass={s.dot}
                  pulse={!!s.pulse}
                  statusLabel={s.label}
                  statusTextClass={s.text}
                  titleFallback={taskPromptText(t)}
                  subtitle={t.title ? taskPromptText(t) : null}
                  onOpen={() => setOpenTaskId(t.id)}
                  onReload={load}
                  actions={actions}
                />
                <div
                  role="button"
                  tabIndex={0}
                  className="hidden w-full cursor-pointer gap-3 px-3 py-3 text-left hover:bg-dh-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-dh-accent md:grid md:grid-cols-[112px_120px_minmax(0,1fr)_96px_92px_156px] md:items-center"
                  onClick={() => setOpenTaskId(t.id)}
                  onKeyDown={(event) => {
                    if (event.target !== event.currentTarget || (event.key !== "Enter" && event.key !== " ")) return;
                    event.preventDefault();
                    setOpenTaskId(t.id);
                  }}
                >
                  <div className="flex items-center gap-2">
                    <span className={`h-2.5 w-2.5 rounded-full ${s.dot} ${s.pulse ? "animate-pulse" : ""}`} />
                    <span className={`text-xs font-medium ${s.text}`}>{s.label}</span>
                  </div>
                  <div className="min-w-0 truncate text-xs text-dh-muted" title={p?.path}>
                    {p?.name ?? `项目 ${t.project_id}`}
                  </div>
                  <div className="min-w-0">
                    <div className="flex min-w-0 items-center gap-2">
                      <span className="shrink-0 font-mono text-xs text-slate-400">#{t.id}</span>
                      <EngineBadge engine={t.engine} />
                      <EditableTaskTitle task={t} fallback={taskPromptText(t)} onSaved={load} />
                    </div>
                    {t.title && <div className="mt-0.5 truncate text-xs text-slate-400">{taskPromptText(t)}</div>}
                  </div>
                  <div className="text-xs tabular-nums text-dh-muted">
                    {fmtDur(t.started_at, t.ended_at, t.elapsed_accum)}
                  </div>
                  <div className="text-xs tabular-nums text-dh-muted">
                    {t.tokens != null ? t.tokens.toLocaleString() : "—"}
                  </div>
                  <div className="flex items-center justify-end gap-2 text-xs font-medium">
                    {actions}
                  </div>
                </div>
                </DraggableTaskRow>
              );
            })}
          </div>
        </div>
      )}

      <PausedDropZone>
        <div className="flex items-end gap-3 px-1">
          <div>
            <div className="flex items-center gap-2 text-sm font-semibold text-dh-text">
              <span>等待执行</span>
              <span className="rounded-full bg-violet-500/15 px-2 py-0.5 text-[10px] font-medium text-violet-300 ring-1 ring-dh-border">
                {pausedTasks.length}
              </span>
            </div>
            <div className="mt-1 text-xs text-slate-400">不会自动运行 · 拖入任务留待后续继续</div>
          </div>
        </div>
        {pausedTasks.length === 0 ? (
          <div className="flex min-h-28 items-center justify-center rounded-xl border border-dashed border-violet-500/40 bg-violet-500/10 px-4 text-center text-sm text-slate-400">
            把运行中或归档任务拖到这里
          </div>
        ) : (
          <div className="overflow-hidden rounded-xl border border-violet-500/40 bg-dh-surface">
            <div className="hidden grid-cols-[96px_120px_minmax(0,1fr)_96px_92px_176px] gap-3 border-b border-violet-500/25 bg-violet-500/10 px-3 py-2 text-xs font-medium text-slate-400 md:grid">
              <span>状态</span><span>项目</span><span>任务</span><span>时长</span><span>Token</span><span className="text-right">操作</span>
            </div>
            <div className="divide-y divide-violet-500/25">
              {pausedTasks.map((task) => {
                const project = projectById.get(task.project_id);
                const restoringToArchive = ARCHIVED_STATUSES.has(task.paused_from_status ?? "");
                const busy = transitioningTaskId !== null || completingTaskId !== null;
                const actions = (
                  <>
                    <button
                      type="button"
                      className={RESUME_BTN_CLS}
                      disabled={busy}
                      onClick={(event) => {
                        event.stopPropagation();
                        void resumePausedTask(task);
                      }}
                    >
                      {transitioningTaskId === task.id ? "处理中…" : "继续执行"}
                    </button>
                    <button
                      type="button"
                      className="group/complete inline-flex items-center gap-1 rounded-md px-2 py-1.5 text-dh-muted transition-colors hover:bg-emerald-500/20 hover:text-emerald-300 disabled:cursor-wait disabled:opacity-50"
                      disabled={busy}
                      title="标记为完成并移入归档"
                      onClick={(event) => {
                        event.stopPropagation();
                        void completeTask(task);
                      }}
                    >
                      {completingTaskId === task.id ? (
                        "处理中…"
                      ) : (
                        <>
                          <span className="grid h-4 w-4 place-items-center rounded-full border border-dh-bsoft text-[10px] leading-none transition-colors group-hover/complete:border-emerald-500/40">
                            ✓
                          </span>
                          <span>完成</span>
                        </>
                      )}
                    </button>
                    <button
                      type="button"
                      className="rounded-md px-2 py-1.5 text-dh-muted hover:bg-dh-hover hover:text-slate-50 disabled:opacity-50"
                      disabled={busy}
                      onClick={(event) => {
                        event.stopPropagation();
                        void restorePausedTask(task);
                      }}
                    >
                      {restoringToArchive ? "移回归档" : "移回待办"}
                    </button>
                  </>
                );
                return (
                  <div key={task.id}>
                  <MobileTaskCard
                    task={task}
                    projectName={project?.name ?? `项目 ${task.project_id}`}
                    projectPath={project?.path}
                    dotClass="bg-violet-400"
                    statusLabel="等待执行"
                    statusTextClass="text-violet-300"
                    titleFallback={taskPromptText(task)}
                    subtitle={taskPromptText(task)}
                    onOpen={() => setOpenTaskId(task.id)}
                    onReload={load}
                    actions={actions}
                  />
                  <div
                    role="button"
                    tabIndex={0}
                    className="hidden gap-3 px-3 py-3 text-left hover:bg-violet-500/20 focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-violet-500 md:grid md:grid-cols-[96px_120px_minmax(0,1fr)_96px_92px_248px] md:items-center"
                    onClick={() => setOpenTaskId(task.id)}
                    onKeyDown={(event) => {
                      if (event.target !== event.currentTarget || (event.key !== "Enter" && event.key !== " ")) return;
                      event.preventDefault();
                      setOpenTaskId(task.id);
                    }}
                  >
                    <div className="flex items-center gap-2">
                      <span className="h-2.5 w-2.5 rounded-full bg-violet-400" />
                      <span className="text-xs font-medium text-violet-300">等待执行</span>
                    </div>
                    <div className="min-w-0 truncate text-xs text-dh-muted" title={project?.path}>
                      {project?.name ?? `项目 ${task.project_id}`}
                    </div>
                    <div className="min-w-0">
                      <div className="flex min-w-0 items-center gap-2">
                        <span className="shrink-0 font-mono text-xs text-slate-400">#{task.id}</span>
                        <EngineBadge engine={task.engine} />
                        <EditableTaskTitle task={task} fallback={taskPromptText(task)} onSaved={load} />
                      </div>
                      <div className="mt-0.5 truncate text-xs text-slate-400">{taskPromptText(task)}</div>
                    </div>
                    <div className="text-xs tabular-nums text-dh-muted">{fmtDur(task.started_at, task.ended_at, task.elapsed_accum)}</div>
                    <div className="text-xs tabular-nums text-dh-muted">{task.tokens != null ? task.tokens.toLocaleString() : "—"}</div>
                    <div className="flex items-center justify-end gap-1.5 text-xs font-medium">
                      {actions}
                    </div>
                  </div>
                  </div>
                );
              })}
            </div>
          </div>
        )}
      </PausedDropZone>

      {totalArchivedCount > 0 && (
        <section className="space-y-3 border-t border-dh-bsoft pt-5">
          <div className="flex flex-wrap items-end gap-3">
            <div>
              <div className="text-sm font-semibold text-dh-text">归档任务</div>
              <div className="mt-1 text-xs text-slate-400">{totalArchivedCount} 个过往任务 · 按日期倒序</div>
            </div>
            <select
              aria-label="按项目筛选归档任务"
              value={archiveProjectId}
              onChange={(event) => setArchiveProjectId(event.target.value)}
              className="w-full rounded-lg border border-dh-bsoft bg-dh-surface px-3 py-2 text-sm text-dh-tsoft outline-none focus:border-dh-m2 sm:ml-auto sm:w-44"
            >
              <option value="">全部项目</option>
              {archivedProjects.map((project) => (
                <option key={project.id} value={project.id}>{project.name}</option>
              ))}
            </select>
            <div className="relative min-w-0 flex-1 sm:max-w-md">
              <span className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-sm text-slate-400">⌕</span>
              <input
                type="search"
                value={archiveQuery}
                onChange={(event) => setArchiveQuery(event.target.value)}
                placeholder="搜索任务编号、项目、标题或指令…"
                className="w-full rounded-lg border border-dh-bsoft bg-dh-surface py-2 pl-8 pr-3 text-sm text-dh-tsoft outline-none focus:border-dh-m2"
              />
            </div>
            {(archiveQuery.trim() || archiveProjectId) && (
              <span className="shrink-0 pb-2 text-xs text-slate-400">找到 {archivedCount} 个</span>
            )}
          </div>
          {archivedCount === 0 ? (
            <div className="rounded-xl border border-dashed border-dh-bsoft bg-dh-soft px-4 py-10 text-center text-sm text-slate-400">
              没有匹配的归档任务
            </div>
          ) : (
            <div className="space-y-3">
              {visibleArchivedGroups.map((group, groupIndex) => {
                const expanded = isArchiveDateOpen(group.date, groupIndex);
                return (
                <div key={group.date} className="overflow-hidden rounded-xl border border-dh-bsoft bg-dh-surface">
                  <button
                    type="button"
                    aria-expanded={expanded}
                    onClick={() => toggleArchiveDate(group.date, groupIndex)}
                    className={`flex w-full items-center px-3 py-2 text-left hover:bg-dh-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-dh-accent ${expanded ? "border-b border-dh-bsoft bg-dh-soft" : "bg-dh-soft"}`}
                  >
                    <span className={`mr-2 text-[10px] text-slate-400 transition-transform ${expanded ? "rotate-90" : ""}`}>▶</span>
                    <span className="text-xs font-semibold text-dh-tsoft">{group.date}</span>
                    <span className="ml-auto rounded-full bg-dh-s2 px-2 py-0.5 text-[10px] font-semibold text-slate-100 ring-1 ring-inset ring-slate-700">
                      {group.tasks.length}
                    </span>
                  </button>
                  <div className={`divide-y divide-dh-bsoft ${expanded ? "" : "hidden"}`}>
                    {group.tasks.map((task) => {
                      const project = projectById.get(task.project_id);
                      const style = STATUS_STYLE[statusKey(task)] ?? STATUS_STYLE.done;
                      const actions = (
                        <>
                          <span className="text-dh-tsoft">查看记录</span>
                          <button
                            type="button"
                            className={RESUME_BTN_CLS}
                            disabled={transitioningTaskId !== null || !task.session_uid}
                            title={
                              task.session_uid
                                ? "恢复原会话继续执行（可追加指令）"
                                : "该任务没有可恢复的会话，无法原地继续"
                            }
                            onClick={(event) => {
                              event.stopPropagation();
                              void continueArchivedTask(task);
                            }}
                          >
                            {transitioningTaskId === task.id ? "处理中…" : "继续执行"}
                          </button>
                          <button
                            type="button"
                            className="rounded-md px-1.5 py-1 text-[11px] font-normal text-violet-300 hover:bg-violet-500/20 hover:text-violet-300 disabled:opacity-50"
                            disabled={transitioningTaskId !== null}
                            onClick={(event) => {
                              event.stopPropagation();
                              void pauseTask(task);
                            }}
                          >
                            等待
                          </button>
                        </>
                      );
                      return (
                        <DraggableTaskRow key={task.id} task={task}>
                        <MobileTaskCard
                          task={task}
                          projectName={project?.name ?? `项目 ${task.project_id}`}
                          projectPath={project?.path}
                          dotClass={style.dot}
                          statusLabel={style.label}
                          statusTextClass={style.text}
                          titleFallback={taskPromptText(task)}
                          subtitle={task.title ? taskPromptText(task) : null}
                          onOpen={() => setOpenTaskId(task.id)}
                          onReload={load}
                          actions={actions}
                        />
                        <div
                          role="button"
                          tabIndex={0}
                          className="hidden w-full gap-3 px-3 py-3 text-left hover:bg-dh-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-dh-accent md:grid md:grid-cols-[96px_120px_minmax(0,1fr)_96px_92px_132px] md:items-center"
                          onClick={() => setOpenTaskId(task.id)}
                          onKeyDown={(event) => {
                            if (event.target !== event.currentTarget || (event.key !== "Enter" && event.key !== " ")) return;
                            event.preventDefault();
                            setOpenTaskId(task.id);
                          }}
                        >
                          <div className="flex items-center gap-2">
                            <span className={`h-2.5 w-2.5 rounded-full ${style.dot}`} />
                            <span className={`text-xs font-medium ${style.text}`}>{style.label}</span>
                          </div>
                          <div className="min-w-0 truncate text-xs text-dh-muted" title={project?.path}>
                            {project?.name ?? `项目 ${task.project_id}`}
                          </div>
                          <div className="min-w-0">
                            <div className="flex min-w-0 items-center gap-2">
                              <span className="shrink-0 font-mono text-xs text-slate-400">#{task.id}</span>
                              <EngineBadge engine={task.engine} />
                              <EditableTaskTitle task={task} fallback={taskPromptText(task)} onSaved={load} />
                            </div>
                            {task.title && <div className="mt-0.5 truncate text-xs text-slate-400">{taskPromptText(task)}</div>}
                          </div>
                          <div className="text-xs tabular-nums text-dh-muted">
                            {fmtDur(task.started_at, task.ended_at, task.elapsed_accum)}
                          </div>
                          <div className="text-xs tabular-nums text-dh-muted">
                            {task.tokens != null ? task.tokens.toLocaleString() : "—"}
                          </div>
                          <div className="flex items-center justify-end gap-2 text-xs font-medium">
                            {actions}
                          </div>
                        </div>
                        </DraggableTaskRow>
                      );
                    })}
                  </div>
                </div>
                );
              })}
              {olderGroups.length > 0 && (
                <button
                  type="button"
                  onClick={() => setShowOlderArchives(true)}
                  className="w-full rounded-xl border border-dashed border-dh-bsoft bg-dh-soft px-4 py-2.5 text-center text-xs font-medium text-dh-muted hover:bg-dh-hover hover:text-slate-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-dh-accent"
                >
                  显示更早的归档 · {olderGroups.length} 天 / {olderTaskCount} 个任务
                </button>
              )}
            </div>
          )}
        </section>
      )}

        </div>
      </div>
      {activeWorkspace && (
        <div className="min-h-0 flex-1">
          <TerminalWorkspace
            inline
            taskIds={activeWorkspace.taskIds}
            tasks={tasks}
            layout={activeWorkspace.layout}
            switchTasks={switchTasks}
            onLayoutChange={changeLayout}
            onCloseTask={closeTask}
            onCloseAll={() =>
              setWorkspaces((prev) =>
                prev.map((w) => (w.id === activeWorkspace.id ? { ...w, taskIds: [] } : w)),
              )
            }
            onSwitchTask={(t) => {
              setTasks((current) => current.some((item) => item.id === t.id) ? current : [...current, t]);
              openTask(t.id);
            }}
            getProjectName={(projectId) => projectById.get(projectId)?.name ?? `项目 ${projectId}`}
            onChanged={changed}
          />
        </div>
      )}
      {/* 列表里点单个任务：单终端抽屉（不进工作台） */}
      {!activeWorkspace && openTaskId != null && (() => {
        const openOne = tasks.find((t) => t.id === openTaskId);
        if (!openOne) return null;
        return (
          <TerminalPanel
            task={openOne}
            switchTasks={activeTasks}
            onSwitchTask={(t) => {
              setTasks((current) => current.some((item) => item.id === t.id) ? current : [...current, t]);
              setOpenTaskId(t.id);
            }}
            getProjectName={(projectId) => projectById.get(projectId)?.name ?? `项目 ${projectId}`}
            onClose={() => setOpenTaskId(null)}
            onChanged={changed}
          />
        );
      })()}
      {showCreate && (
        <CreateTaskDialog
          projects={projects}
          onClose={() => setShowCreate(false)}
          onCreated={changed}
        />
      )}
    </div>
    </DndContext>
  );
}
