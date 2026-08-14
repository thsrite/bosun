import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import type { OrchestrationRun, Project, Task } from "../types";
import { AutopilotDialog } from "./AutopilotDialog";
import { CreateTaskDialog } from "./CreateTaskDialog";
import { FindingsInbox } from "./FindingsInbox";
import { SourcesDialog } from "./SourcesDialog";
import { ImportSessionDialog } from "./ImportSessionDialog";
import { TaskBoard } from "./TaskBoard";
import { OrchestrationRunList } from "./OrchestrationRunList";
import { TerminalPanel } from "./TerminalPanel";
import { guardQuota } from "../quota";
import { useSingleFlight } from "../useSingleFlight";
import { taskPromptText } from "../taskText";

export function ProjectView({
  project,
  reloadKey,
  onBack,
  onDataChanged,
  onDeleteProject,
}: {
  project: Project;
  reloadKey: number;
  onBack: () => void;
  onDataChanged: () => void;
  onDeleteProject: (project: Project) => void;
}) {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [orchestrationRuns, setOrchestrationRuns] = useState<OrchestrationRun[]>([]);
  const [openTerminal, setOpenTerminal] = useState<Task | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [showFindings, setShowFindings] = useState(false);
  const [showImport, setShowImport] = useState(false);
  const [showAutopilot, setShowAutopilot] = useState(false);
  const [showSources, setShowSources] = useState(false);
  const [taskQuery, setTaskQuery] = useState("");
  const { busy: startingAll, run: runStartAll } = useSingleFlight();

  const load = useCallback(async () => {
    const [nextTasks, nextRuns] = await Promise.all([
      api.tasks(project.id),
      api.orchestrationRuns.list(project.id),
    ]);
    setTasks(nextTasks);
    setOrchestrationRuns(nextRuns);
  }, [project.id]);

  useEffect(() => {
    load();
  }, [load, reloadKey]);

  const changed = () => {
    load();
    onDataChanged();
  };
  const draftCount = tasks.filter((t) => t.status === "draft").length;
  const matchedTaskCount = useMemo(() => {
    const query = taskQuery.trim().toLocaleLowerCase();
    if (!query) return tasks.length;
    return tasks.filter((task) =>
      `#${task.id}\n${task.id}\n${task.title ?? ""}\n${taskPromptText(task)}`.toLocaleLowerCase().includes(query)
    ).length;
  }, [taskQuery, tasks]);
  const currentTerminalTask = openTerminal
    ? tasks.find((t) => t.id === openTerminal.id) ?? openTerminal
    : null;
  const terminalSwitchTasks = currentTerminalTask
    ? tasks.filter(
        (t) =>
          ["queued", "running", "waiting_input", "rate_limited"].includes(t.status) ||
          t.id === currentTerminalTask.id,
      )
    : [];

  return (
    <div className="space-y-5 px-4 py-5 md:px-8 md:py-6">
      <div className="flex flex-wrap items-center gap-2 md:gap-3">
        <span className="hidden max-w-[40%] truncate text-xs text-slate-400 md:inline" title={project.path}>
          {project.path}
        </span>
        <span className="shrink-0 text-xs text-dh-muted">
          {project.task_total} 任务
          {project.running > 0 && (
            <span className="ml-1 text-emerald-300">· ●{project.running} 运行</span>
          )}
        </span>
        <div className="dh-scrollbar-none flex w-full gap-2 overflow-x-auto pb-1 md:ml-auto md:w-auto md:flex-wrap md:overflow-visible md:pb-0">
          <button
            className="shrink-0 whitespace-nowrap rounded-lg bg-dh-accent px-3 py-1.5 text-sm font-medium text-dh-accfg shadow-sm shadow-black/30 hover:bg-dh-acchov"
            onClick={() => setShowCreate(true)}
          >
            + 任务
          </button>
          {draftCount > 0 && (
            <button
              className="shrink-0 whitespace-nowrap rounded-lg bg-emerald-500 px-3 py-1.5 text-sm font-medium text-white hover:bg-emerald-600 disabled:opacity-50"
              disabled={startingAll}
              onClick={() =>
                runStartAll(async () => {
                  if (!(await guardQuota())) return;
                  await api.startAll(project.id);
                  changed();
                })
              }
            >
              ▶ 执行待办 ({draftCount})
            </button>
          )}
          <button
            className="relative shrink-0 whitespace-nowrap rounded-lg border border-dh-bsoft bg-dh-surface px-3 py-1.5 text-sm text-dh-tsoft hover:bg-dh-hover"
            onClick={() => setShowFindings(true)}
          >
            整体分析
            {project.open_findings > 0 && (
              <span className="absolute -right-1.5 -top-1.5 rounded-full bg-amber-400 px-1.5 text-[10px] font-medium text-white">
                {project.open_findings}
              </span>
            )}
          </button>
          <button
            className="shrink-0 whitespace-nowrap rounded-lg border border-emerald-500/40 bg-emerald-500/10 px-3 py-1.5 text-sm text-emerald-300 hover:bg-emerald-500/20"
            onClick={() => setShowAutopilot(true)}
            title="AI 自审自修：审→修→验→交叉复审 循环"
          >
            🤖 自动修复
          </button>
          <button
            className="shrink-0 whitespace-nowrap rounded-lg border border-dh-bsoft bg-dh-surface px-3 py-1.5 text-sm text-dh-tsoft hover:bg-dh-hover"
            onClick={() => setShowSources(true)}
            title="配置外部 API 问题来源，拉取条目进收件箱"
          >
            🔌 问题来源
          </button>
          <button
            className="shrink-0 whitespace-nowrap rounded-lg border border-dh-bsoft bg-dh-surface px-3 py-1.5 text-sm text-dh-tsoft hover:bg-dh-hover"
            onClick={() => setShowImport(true)}
            title="导入别人分享的会话"
          >
            导入会话
          </button>
          <button
            className="shrink-0 whitespace-nowrap rounded-lg border border-rose-500/40 bg-dh-surface px-3 py-1.5 text-sm text-rose-400 hover:bg-rose-500/20"
            onClick={() => onDeleteProject(project)}
            title="从 Bosun 删除此项目，不删除磁盘目录"
          >
            删除项目
          </button>
        </div>
      </div>

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
          <span className="shrink-0 text-xs text-slate-400">找到 {matchedTaskCount} 个</span>
        )}
      </div>

      <TaskBoard
        tasks={tasks}
        query={taskQuery}
        setTasks={setTasks}
        onOpenTerminal={setOpenTerminal}
        onChanged={changed}
      />

      <OrchestrationRunList runs={orchestrationRuns} onChanged={changed} />

      {currentTerminalTask && (
        <TerminalPanel
          task={currentTerminalTask}
          switchTasks={terminalSwitchTasks}
          onSwitchTask={setOpenTerminal}
          getProjectName={() => project.name}
          onClose={() => setOpenTerminal(null)}
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
