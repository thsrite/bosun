import { useCallback, useEffect, useMemo, useState } from "react";
import { api, showHiddenProjects } from "../api";
import { STATUS_STYLE, taskStatusStyleKey } from "../theme";
import type { OrchestrationRun, Project, Task } from "../types";
import { AutopilotDialog } from "./AutopilotDialog";
import { CreateTaskDialog } from "./CreateTaskDialog";
import { FindingsInbox } from "./FindingsInbox";
import { SourcesDialog } from "./SourcesDialog";
import { ImportSessionDialog } from "./ImportSessionDialog";
import { TaskBoard } from "./TaskBoard";
import { OrchestrationRunList } from "./OrchestrationRunList";
import { OrchestrationTrace, statusText } from "./OrchestrationTrace";
import { TerminalPanel } from "./TerminalPanel";
import { guardQuota } from "../quota";
import { confirmDialog, toast } from "../overlay";
import { useSingleFlight } from "../useSingleFlight";
import { taskPromptText } from "../taskText";

type LayoutMode = "workspace" | "board";
type StageSelection =
  | { kind: "task"; id: number; fromRunId?: number }
  | { kind: "run"; id: number }
  | null;

const LAYOUT_KEY = "bosun.projectLayout";
const ACTIVE_STATUSES = new Set(["running", "waiting_input", "queued"]);

function loadLayoutMode(): LayoutMode {
  try {
    return localStorage.getItem(LAYOUT_KEY) === "board" ? "board" : "workspace";
  } catch {
    return "workspace";
  }
}

function runSnapshotName(run: OrchestrationRun): string {
  try {
    return (JSON.parse(run.definition_snapshot) as { name?: string }).name || "任务编排";
  } catch {
    return "任务编排";
  }
}

function taskSortWeight(t: Task): number {
  if (t.status === "waiting_input") return 0;
  if (t.status === "running") return 1;
  if (t.status === "queued") return 2;
  if (t.status === "draft") return 3;
  return 4;
}

export function ProjectView({
  project,
  reloadKey,
  onBack,
  onDataChanged,
  onDeleteProject,
  onToggleProjectHidden,
}: {
  project: Project;
  reloadKey: number;
  onBack: () => void;
  onDataChanged: () => void;
  onDeleteProject: (project: Project) => void;
  onToggleProjectHidden: (project: Project) => void;
}) {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [orchestrationRuns, setOrchestrationRuns] = useState<OrchestrationRun[]>([]);
  const [layoutMode, setLayoutMode] = useState<LayoutMode>(loadLayoutMode);
  const [stage, setStage] = useState<StageSelection>(null);
  const [openDrawerTask, setOpenDrawerTask] = useState<Task | null>(null);
  const [expandedParents, setExpandedParents] = useState<Record<number, boolean>>({});
  const [showCreate, setShowCreate] = useState(false);
  const [showFindings, setShowFindings] = useState(false);
  const [showImport, setShowImport] = useState(false);
  const [showAutopilot, setShowAutopilot] = useState(false);
  const [showSources, setShowSources] = useState(false);
  const [showMore, setShowMore] = useState(false);
  const [taskQuery, setTaskQuery] = useState("");
  const { busy: actionBusy, run: runAction } = useSingleFlight();

  const load = useCallback(async () => {
    const [nextTasks, nextRuns] = await Promise.all([
      api.tasks(project.id),
      api.orchestrationRuns.list(project.id),
    ]);
    setTasks(nextTasks);
    setOrchestrationRuns(nextRuns);
  }, [project.id]);

  useEffect(() => {
    void load();
  }, [load, reloadKey]);

  // 切换项目时重置选中项
  useEffect(() => {
    setStage(null);
    setOpenDrawerTask(null);
  }, [project.id]);

  // 工作台模式下自动选中 URL 指定的编排或最优先的活动任务，避免进来大面积留白
  useEffect(() => {
    if (layoutMode !== "workspace" || stage !== null) return;
    const linkedRun = Number(new URLSearchParams(location.search).get("run"));
    if (linkedRun && orchestrationRuns.some((r) => r.id === linkedRun)) {
      setStage({ kind: "run", id: linkedRun });
      return;
    }
    const activeRun = orchestrationRuns.find((r) => ACTIVE_STATUSES.has(r.status));
    if (activeRun) {
      setStage({ kind: "run", id: activeRun.id });
      return;
    }
    const sorted = [...tasks]
      .filter((t) => t.status !== "paused" && t.parent_task_id == null)
      .sort((a, b) => taskSortWeight(a) - taskSortWeight(b) || b.id - a.id);
    if (sorted[0]) {
      setStage({ kind: "task", id: sorted[0].id });
    } else if (orchestrationRuns[0]) {
      setStage({ kind: "run", id: orchestrationRuns[0].id });
    }
  }, [layoutMode, stage, tasks, orchestrationRuns]);

  function switchLayout(mode: LayoutMode) {
    setLayoutMode(mode);
    try {
      localStorage.setItem(LAYOUT_KEY, mode);
    } catch {
      // 忽略存储限制
    }
  }

  const changed = () => {
    void load();
    onDataChanged();
  };

  const draftCount = tasks.filter((t) => t.status === "draft").length;
  const normalizedQuery = taskQuery.trim().toLocaleLowerCase();

  const visibleTasks = useMemo(() => {
    const base = tasks.filter((t) => t.status !== "paused");
    if (!normalizedQuery) return base;
    return base.filter((task) =>
      `#${task.id}\n${task.id}\n${task.title ?? ""}\n${taskPromptText(task)}`
        .toLocaleLowerCase()
        .includes(normalizedQuery),
    );
  }, [tasks, normalizedQuery]);

  const visibleRuns = useMemo(() => {
    if (!normalizedQuery) return orchestrationRuns;
    return orchestrationRuns.filter((r) =>
      `#${r.id}\n${r.title ?? ""}\n${runSnapshotName(r)}\n${r.original_prompt}`
        .toLocaleLowerCase()
        .includes(normalizedQuery),
    );
  }, [orchestrationRuns, normalizedQuery]);

  // 侧边栏主任务与收敛子任务
  const { rootTasks, childrenByParent } = useMemo(() => {
    const visIds = new Set(visibleTasks.map((t) => t.id));
    const map = new Map<number, Task[]>();
    const roots: Task[] = [];
    for (const t of visibleTasks) {
      if (t.parent_task_id != null && visIds.has(t.parent_task_id)) {
        const arr = map.get(t.parent_task_id) ?? [];
        arr.push(t);
        map.set(t.parent_task_id, arr);
      } else {
        roots.push(t);
      }
    }
    for (const arr of map.values()) arr.sort((a, b) => a.id - b.id);
    roots.sort((a, b) => taskSortWeight(a) - taskSortWeight(b) || b.id - a.id);
    return { rootTasks: roots, childrenByParent: map };
  }, [visibleTasks]);

  const activeStageTask =
    stage?.kind === "task" ? tasks.find((t) => t.id === stage.id) ?? null : null;
  const activeStageRun =
    stage?.kind === "run" ? orchestrationRuns.find((r) => r.id === stage.id) ?? null : null;
  const currentDrawerTask = openDrawerTask
    ? tasks.find((t) => t.id === openDrawerTask.id) ?? openDrawerTask
    : null;

  const terminalSwitchTasksFor = (target: Task | null) =>
    target
      ? tasks.filter(
          (t) =>
            ["queued", "running", "waiting_input"].includes(t.status) ||
            t.id === target.id ||
            t.parent_task_id === (target.parent_task_id ?? target.id) ||
            t.id === target.parent_task_id,
        )
      : [];

  function openTaskFromStage(t: Task, fromRunId?: number) {
    // 桌面工作台模式下直接切到中间嵌入式终端；手机或看板模式打开抽屉终端
    if (layoutMode === "workspace" && window.innerWidth >= 1024) {
      setStage({ kind: "task", id: t.id, fromRunId });
    } else {
      setOpenDrawerTask(t);
    }
  }

  async function runOrchestrationAction(run: OrchestrationRun, op: "start" | "resume" | "cancel") {
    if (op === "cancel") {
      const ok = await confirmDialog(`取消编排 #${run.id}「${run.title || runSnapshotName(run)}」？`, { danger: true });
      if (!ok) return;
    }
    await runAction(async () => {
      try {
        if (op === "start") await api.orchestrationRuns.start(run.id);
        else if (op === "resume") await api.orchestrationRuns.resume(run.id);
        else await api.orchestrationRuns.cancel(run.id);
        changed();
      } catch (err) {
        toast(`操作失败：${err instanceof Error ? err.message : String(err)}`, "error");
      }
    });
  }

  const menuItemCls =
    "block w-full rounded-md px-3 py-2 text-left text-xs text-dh-tsoft hover:bg-dh-hover hover:text-dh-text";

  return (
    <div
      className={
        layoutMode === "workspace"
          ? "flex h-full min-h-0 flex-col overflow-hidden"
          : "space-y-4 px-4 py-4 md:space-y-5 md:px-8 md:py-6"
      }
    >
      {/* 顶部极简工具栏（AGY 客户端风格） */}
      <div
        className={`flex flex-wrap items-center gap-2 ${
          layoutMode === "workspace"
            ? "shrink-0 border-b border-dh-bsoft bg-dh-surface/90 px-4 py-2.5"
            : ""
        }`}
      >
        <button
          type="button"
          className="-ml-1 shrink-0 rounded-md px-1.5 py-1 text-base leading-none text-dh-tsoft hover:bg-dh-hover sm:hidden"
          onClick={onBack}
          aria-label="返回项目列表"
        >
          ‹
        </button>

        <div className="min-w-0 flex items-center gap-2">
          <span className="truncate text-sm font-semibold text-dh-text">{project.name}</span>
          <span className="hidden max-w-[260px] truncate font-mono text-[11px] text-dh-muted xl:inline" title={project.path}>
            {project.path}
          </span>
          <span className="hidden shrink-0 rounded-full bg-dh-s2 px-2 py-0.5 text-[11px] text-dh-muted sm:inline">
            {project.task_total} 任务
            {project.running > 0 && <span className="ml-1 text-emerald-400">· ● {project.running}</span>}
          </span>
        </div>

        {/* 视图切换：工作台（左会话 + 中终端） vs 经典三列看板 */}
        <div className="hidden items-center rounded-lg border border-dh-bsoft bg-dh-soft p-0.5 text-xs lg:inline-flex">
          <button
            type="button"
            onClick={() => switchLayout("workspace")}
            className={`rounded-md px-2.5 py-1 transition ${
              layoutMode === "workspace"
                ? "bg-dh-surface font-medium text-dh-text shadow-sm"
                : "text-dh-muted hover:text-dh-text"
            }`}
          >
            工作台
          </button>
          <button
            type="button"
            onClick={() => switchLayout("board")}
            className={`rounded-md px-2.5 py-1 transition ${
              layoutMode === "board"
                ? "bg-dh-surface font-medium text-dh-text shadow-sm"
                : "text-dh-muted hover:text-dh-text"
            }`}
          >
            看板
          </button>
        </div>

        <div className="ml-auto flex items-center gap-2">
          <button
            type="button"
            className="shrink-0 whitespace-nowrap rounded-lg bg-dh-accent px-3 py-1.5 text-xs font-medium text-dh-accfg shadow-sm hover:bg-dh-acchov"
            onClick={() => setShowCreate(true)}
          >
            + 新建任务
          </button>

          {draftCount > 0 && (
            <button
              type="button"
              className="shrink-0 whitespace-nowrap rounded-lg bg-emerald-600 px-2.5 py-1.5 text-xs font-medium text-white hover:bg-emerald-500 disabled:opacity-50"
              disabled={actionBusy}
              onClick={() =>
                void runAction(async () => {
                  if (!(await guardQuota())) return;
                  await api.startAll(project.id);
                  changed();
                })
              }
            >
              ▶ 执行待办 ({draftCount})
            </button>
          )}

          <div className="relative">
            <button
              type="button"
              onClick={() => setShowMore((v) => !v)}
              className="relative shrink-0 whitespace-nowrap rounded-lg border border-dh-bsoft bg-dh-surface px-2.5 py-1.5 text-xs text-dh-tsoft hover:bg-dh-hover"
            >
              工具 {showMore ? "▴" : "▾"}
              {project.open_findings > 0 && (
                <span className="ml-1 rounded-full bg-amber-400 px-1.5 text-[10px] font-medium text-slate-950">
                  {project.open_findings}
                </span>
              )}
            </button>
            {showMore && (
              <>
                <div className="fixed inset-0 z-20" onClick={() => setShowMore(false)} />
                <div
                  className="absolute right-0 top-full z-30 mt-1.5 w-44 rounded-xl border border-dh-bsoft bg-dh-surface p-1 shadow-xl"
                  onClick={() => setShowMore(false)}
                >
                  <button type="button" className={menuItemCls} onClick={() => setShowFindings(true)}>
                    整体分析 {project.open_findings > 0 ? `(${project.open_findings})` : ""}
                  </button>
                  <button type="button" className={menuItemCls} onClick={() => setShowAutopilot(true)}>
                    🤖 自动修复
                  </button>
                  <button type="button" className={menuItemCls} onClick={() => setShowSources(true)}>
                    🔌 问题来源
                  </button>
                  <button type="button" className={menuItemCls} onClick={() => setShowImport(true)}>
                    导入会话
                  </button>
                  <div className="my-1 border-t border-dh-bsoft" />
                  {showHiddenProjects() && (
                    <button type="button" className={menuItemCls} onClick={() => onToggleProjectHidden(project)}>
                      {project.hidden ? "取消隐藏项目" : "隐藏项目"}
                    </button>
                  )}
                  <button
                    type="button"
                    className="block w-full rounded-md px-3 py-2 text-left text-xs text-rose-400 hover:bg-rose-500/15"
                    onClick={() => onDeleteProject(project)}
                  >
                    删除项目
                  </button>
                </div>
              </>
            )}
          </div>
        </div>
      </div>

      {/* 模式一：AGY 客户端式工作台（左：会话/任务/编排列表，中：嵌入式终端或白盒工作流画布） */}
      {layoutMode === "workspace" ? (
        <div className="flex min-h-0 flex-1 overflow-hidden">
          {/* 左侧会话与任务列表 */}
          <aside className="flex w-full shrink-0 flex-col border-r border-dh-bsoft bg-dh-soft/50 lg:w-[300px] xl:w-[330px]">
            <div className="shrink-0 border-b border-dh-bsoft p-2.5">
              <div className="relative">
                <span className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-xs text-dh-muted">⌕</span>
                <input
                  type="search"
                  value={taskQuery}
                  onChange={(e) => setTaskQuery(e.target.value)}
                  placeholder="搜索任务、子任务或编排…"
                  className="w-full rounded-lg border border-dh-bsoft bg-dh-surface py-1.5 pl-7 pr-2.5 text-xs text-dh-text outline-none focus:border-dh-accent"
                />
              </div>
            </div>

            <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-2.5">
              {/* 编排工作流列表 */}
              {visibleRuns.length > 0 && (
                <section className="space-y-1">
                  <div className="px-1.5 text-[10px] font-semibold uppercase tracking-wider text-dh-muted">
                    编排工作流 ({visibleRuns.length})
                  </div>
                  {visibleRuns.map((run) => {
                    const selected = stage?.kind === "run" && stage.id === run.id;
                    const active = ACTIVE_STATUSES.has(run.status);
                    const curStep = run.steps.find((s) => s.position === run.current_position);
                    return (
                      <button
                        key={run.id}
                        type="button"
                        onClick={() => setStage({ kind: "run", id: run.id })}
                        className={`w-full rounded-xl border px-2.5 py-2 text-left transition ${
                          selected
                            ? "border-dh-accent bg-dh-accent/10 shadow-sm"
                            : "border-transparent bg-dh-surface/70 hover:border-dh-bsoft hover:bg-dh-surface"
                        }`}
                      >
                        <div className="flex items-center gap-1.5">
                          <span
                            className={`h-2 w-2 shrink-0 rounded-full ${
                              run.status === "running"
                                ? "animate-pulse bg-emerald-400"
                                : run.status === "waiting_input"
                                  ? "animate-pulse bg-amber-400"
                                  : run.status === "done"
                                    ? "bg-sky-400"
                                    : "bg-slate-500"
                            }`}
                          />
                          <span className="font-mono text-[10px] text-dh-muted">编排 #{run.id}</span>
                          {run.rework_total > 0 && (
                            <span className="rounded bg-amber-500/15 px-1 text-[10px] text-amber-400">
                              返工 {run.rework_total}
                            </span>
                          )}
                          <span className="ml-auto text-[10px] text-dh-muted">{statusText(run.status)}</span>
                        </div>
                        <div className="mt-1 truncate text-xs font-medium text-dh-text">
                          {run.title || runSnapshotName(run)}
                        </div>
                        <div className="mt-0.5 truncate text-[11px] text-dh-muted">
                          {active && curStep ? `持棒：${curStep.name}` : runSnapshotName(run)}
                        </div>
                      </button>
                    );
                  })}
                </section>
              )}

              {/* 主任务列表（子任务收敛挂载在下方） */}
              <section className="space-y-1.5">
                <div className="flex items-center justify-between px-1.5 text-[10px] font-semibold uppercase tracking-wider text-dh-muted">
                  <span>任务会话 ({rootTasks.length})</span>
                  {normalizedQuery && <span>匹配 {visibleTasks.length}</span>}
                </div>

                {rootTasks.length === 0 && (
                  <div className="rounded-xl border border-dashed border-dh-bsoft p-4 text-center text-xs text-dh-muted">
                    暂无任务，点击右上角「+ 新建任务」开始
                  </div>
                )}

                {rootTasks.map((t) => {
                  const st = STATUS_STYLE[taskStatusStyleKey(t)] ?? STATUS_STYLE.draft;
                  const kids = childrenByParent.get(t.id) ?? [];
                  const selected = stage?.kind === "task" && stage.id === t.id;
                  const liveKids = kids.filter((k) => ACTIVE_STATUSES.has(k.status) || k.status === "failed").length;
                  const doneKids = kids.filter((k) => k.status === "done").length;
                  const kidsOpen = expandedParents[t.id] ?? liveKids > 0;

                  return (
                    <div key={t.id} className="space-y-1">
                      <div
                        className={`rounded-xl border transition ${
                          selected
                            ? "border-dh-accent bg-dh-accent/10 shadow-sm"
                            : "border-transparent bg-dh-surface/80 hover:border-dh-bsoft hover:bg-dh-surface"
                        }`}
                      >
                        <button
                          type="button"
                          onClick={() => openTaskFromStage(t)}
                          className="w-full px-2.5 py-2 text-left"
                        >
                          <div className="flex items-center gap-1.5">
                            <span className={`h-2 w-2 shrink-0 rounded-full ${st.dot} ${st.pulse ? "animate-pulse" : ""}`} />
                            <span className="font-mono text-[10px] text-dh-muted">#{t.id}</span>
                            <span className="rounded bg-dh-s2 px-1 text-[10px] uppercase text-dh-muted">{t.engine}</span>
                            <span className={`ml-auto text-[10px] font-medium ${st.text}`}>{st.label}</span>
                          </div>
                          <div className="mt-1 line-clamp-1 text-xs font-medium text-dh-text">
                            {t.title || taskPromptText(t)}
                          </div>
                        </button>

                        {/* 收敛的子任务状态条 */}
                        {kids.length > 0 && (
                          <div className="border-t border-dh-bsoft/60 px-2.5 py-1.5">
                            <button
                              type="button"
                              onClick={() => setExpandedParents((prev) => ({ ...prev, [t.id]: !kidsOpen }))}
                              className="flex w-full items-center gap-1.5 text-[11px] text-dh-muted hover:text-dh-text"
                            >
                              <span>{kidsOpen ? "▾" : "▸"}</span>
                              <span>子任务 {doneKids}/{kids.length}</span>
                              {liveKids > 0 && (
                                <span className="rounded bg-sky-500/15 px-1 text-[10px] text-sky-400">
                                  {liveKids} 活跃
                                </span>
                              )}
                            </button>
                            {kidsOpen && (
                              <div className="mt-1.5 space-y-1 pl-2 border-l border-dh-bsoft">
                                {kids.map((k) => {
                                  const kst = STATUS_STYLE[taskStatusStyleKey(k)] ?? STATUS_STYLE.draft;
                                  const kSelected = stage?.kind === "task" && stage.id === k.id;
                                  return (
                                    <button
                                      key={k.id}
                                      type="button"
                                      onClick={() => openTaskFromStage(k)}
                                      className={`flex w-full items-center gap-1.5 rounded-lg px-2 py-1 text-left text-[11px] transition ${
                                        kSelected
                                          ? "bg-dh-accent/20 font-medium text-dh-text"
                                          : "hover:bg-dh-hover text-dh-tsoft"
                                      }`}
                                    >
                                      <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${kst.dot} ${kst.pulse ? "animate-pulse" : ""}`} />
                                      <span className="shrink-0 font-mono text-[10px] text-dh-muted">#{k.id}</span>
                                      <span className="min-w-0 flex-1 truncate">{k.title || taskPromptText(k)}</span>
                                      <span className={`shrink-0 text-[10px] ${kst.text}`}>{kst.label}</span>
                                    </button>
                                  );
                                })}
                              </div>
                            )}
                          </div>
                        )}
                      </div>
                    </div>
                  );
                })}
              </section>
            </div>
          </aside>

          {/* 中间沉浸式主舞台（桌面端显示：嵌入式终端 或 白盒工作流画布） */}
          <section className="hidden min-w-0 flex-1 flex-col overflow-hidden bg-dh-surface lg:flex">
            {activeStageTask ? (
              <div className="flex h-full min-h-0 flex-col">
                {stage?.kind === "task" && stage.fromRunId && (
                  <div className="flex shrink-0 items-center justify-between border-b border-dh-bsoft bg-dh-soft px-4 py-1.5 text-xs">
                    <button
                      type="button"
                      onClick={() => setStage({ kind: "run", id: stage.fromRunId! })}
                      className="font-medium text-dh-accent hover:underline"
                    >
                      ← 返回编排工作流 #{stage.fromRunId}
                    </button>
                    <span className="text-[11px] text-dh-muted">当前处于编排步骤终端视角</span>
                  </div>
                )}
                <div className="min-h-0 flex-1">
                  <TerminalPanel
                    embedded
                    task={activeStageTask}
                    switchTasks={terminalSwitchTasksFor(activeStageTask)}
                    onSwitchTask={(next) =>
                      setStage({
                        kind: "task",
                        id: next.id,
                        fromRunId: stage?.kind === "task" ? stage.fromRunId : undefined,
                      })
                    }
                    getProjectName={() => project.name}
                    onClose={() => setStage(null)}
                    onChanged={changed}
                  />
                </div>
              </div>
            ) : activeStageRun ? (
              <div className="flex h-full min-h-0 flex-col">
                <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b border-dh-bsoft bg-dh-soft/70 px-4 py-2.5 text-xs">
                  <div className="flex items-center gap-2">
                    <span className="font-mono text-dh-muted">编排 #{activeStageRun.id}</span>
                    <strong className="text-sm text-dh-text">
                      {activeStageRun.title || runSnapshotName(activeStageRun)}
                    </strong>
                    <span className="rounded bg-dh-surface px-2 py-0.5 text-[11px] text-dh-tsoft">
                      {statusText(activeStageRun.status)}
                    </span>
                  </div>
                  <div className="flex items-center gap-2">
                    {activeStageRun.status === "draft" && (
                      <button
                        type="button"
                        disabled={actionBusy}
                        onClick={() => void runOrchestrationAction(activeStageRun, "start")}
                        className="rounded-md bg-dh-accent px-2.5 py-1 font-medium text-dh-accfg disabled:opacity-50"
                      >
                        执行编排
                      </button>
                    )}
                    {ACTIVE_STATUSES.has(activeStageRun.status) && (
                      <>
                        <button
                          type="button"
                          disabled={actionBusy}
                          onClick={() => void runOrchestrationAction(activeStageRun, "resume")}
                          className="rounded-md border border-dh-bsoft bg-dh-surface px-2.5 py-1 text-dh-tsoft disabled:opacity-50"
                        >
                          恢复班组
                        </button>
                        <button
                          type="button"
                          disabled={actionBusy}
                          onClick={() => void runOrchestrationAction(activeStageRun, "cancel")}
                          className="rounded-md border border-rose-500/40 px-2.5 py-1 text-rose-400 disabled:opacity-50"
                        >
                          取消编排
                        </button>
                      </>
                    )}
                  </div>
                </div>
                <div className="min-h-0 flex-1 overflow-y-auto p-4">
                  <OrchestrationTrace
                    initial={activeStageRun}
                    onOpenTask={(t) => openTaskFromStage(t, activeStageRun.id)}
                  />
                </div>
              </div>
            ) : (
              <div className="flex h-full flex-col items-center justify-center gap-3 p-8 text-center">
                <div className="text-sm font-medium text-dh-text">在左侧选择一个任务或编排工作流</div>
                <p className="max-w-sm text-xs text-dh-muted">
                  点击任务或子任务可直接在此处打开嵌入式终端；点击编排工作流可查看全白盒拓扑画布、门禁状态与产品验收卡。
                </p>
                <button
                  type="button"
                  onClick={() => setShowCreate(true)}
                  className="rounded-lg bg-dh-accent px-4 py-2 text-xs font-medium text-dh-accfg hover:bg-dh-acchov"
                >
                  + 新建任务
                </button>
              </div>
            )}
          </section>
        </div>
      ) : (
        /* 模式二：经典三列看板模式 */
        <>
          <div className="flex items-center gap-2">
            <div className="relative min-w-0 flex-1 sm:max-w-md">
              <span className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-sm text-slate-400">⌕</span>
              <input
                type="search"
                value={taskQuery}
                onChange={(event) => setTaskQuery(event.target.value)}
                placeholder="搜索任务编号、标题或指令…"
                className="w-full rounded-lg border border-dh-bsoft bg-dh-surface py-2 pl-8 pr-3 text-sm text-dh-tsoft outline-none focus:border-dh-m2"
              />
            </div>
            {taskQuery.trim() && (
              <span className="shrink-0 text-xs text-slate-400">找到 {visibleTasks.length} 个</span>
            )}
          </div>

          <TaskBoard
            tasks={tasks}
            query={taskQuery}
            setTasks={setTasks}
            onOpenTerminal={setOpenDrawerTask}
            onChanged={changed}
          />

          <OrchestrationRunList runs={orchestrationRuns} onChanged={changed} onOpenTask={setOpenDrawerTask} />
        </>
      )}

      {/* 抽屉终端（看板模式或移动端使用） */}
      {currentDrawerTask && (
        <TerminalPanel
          task={currentDrawerTask}
          switchTasks={terminalSwitchTasksFor(currentDrawerTask)}
          onSwitchTask={setOpenDrawerTask}
          getProjectName={() => project.name}
          onClose={() => setOpenDrawerTask(null)}
          onChanged={changed}
        />
      )}
      {showCreate && (
        <CreateTaskDialog project={project} onClose={() => setShowCreate(false)} onCreated={changed} />
      )}
      {showFindings && (
        <FindingsInbox project={project} onClose={() => setShowFindings(false)} onChanged={changed} />
      )}
      {showImport && (
        <ImportSessionDialog project={project} onClose={() => setShowImport(false)} onDone={changed} />
      )}
      {showSources && (
        <SourcesDialog project={project} onClose={() => setShowSources(false)} onChanged={changed} />
      )}
      {showAutopilot && (
        <AutopilotDialog project={project} onClose={() => setShowAutopilot(false)} />
      )}
    </div>
  );
}
