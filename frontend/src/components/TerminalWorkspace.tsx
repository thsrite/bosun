import { useEffect, useState, type CSSProperties } from "react";
import { STATUS_STYLE, taskStatusStyleKey } from "../theme";
import type { Task } from "../types";
import { TerminalPanel } from "./TerminalPanel";
import { taskPromptText } from "../taskText";

export type WorkspaceLayout = 1 | 2 | 4;

// 桌面端并排，移动端 fallback 到单终端全屏（屏幕放不下宫格）
function useIsDesktop(): boolean {
  const [desktop, setDesktop] = useState(() =>
    typeof window !== "undefined" ? window.matchMedia("(min-width: 1024px)").matches : true,
  );
  useEffect(() => {
    const mq = window.matchMedia("(min-width: 1024px)");
    const update = () => setDesktop(mq.matches);
    update();
    mq.addEventListener?.("change", update);
    return () => mq.removeEventListener?.("change", update);
  }, []);
  return desktop;
}

// 宿主固定在应用头部下方（与单终端抽屉一致）
function useHeaderTop(): number {
  const [top, setTop] = useState(0);
  useEffect(() => {
    const desktop = window.matchMedia("(min-width: 768px)");
    const header = document.querySelector<HTMLElement>(".dh-app-header");
    const update = () =>
      setTop(desktop.matches ? Math.round(header?.getBoundingClientRect().height ?? 0) : 0);
    update();
    const ro = header && typeof ResizeObserver !== "undefined" ? new ResizeObserver(update) : null;
    if (header) ro?.observe(header);
    desktop.addEventListener?.("change", update);
    window.addEventListener("resize", update);
    return () => {
      ro?.disconnect();
      desktop.removeEventListener?.("change", update);
      window.removeEventListener("resize", update);
    };
  }, []);
  return top;
}

// 布局越多，抽屉越宽，给每个终端留出可读宽度
const DRAWER_WIDTH: Record<WorkspaceLayout, string> = {
  1: "lg:w-[48%] lg:min-w-[560px]",
  2: "lg:w-[74%]",
  4: "lg:w-[92%]",
};

const LAYOUT_META: { value: WorkspaceLayout; icon: string; title: string }[] = [
  { value: 1, icon: "▢", title: "单格" },
  { value: 2, icon: "▤", title: "左右两格" },
  { value: 4, icon: "⊞", title: "四宫格" },
];

/** 多终端并排宿主：桌面端 1/2/4 宫格；移动端只显示激活的一个。 */
export function TerminalWorkspace({
  inline = false,
  taskIds,
  tasks,
  layout,
  switchTasks,
  onLayoutChange,
  onCloseTask,
  onCloseAll,
  onSwitchTask,
  getProjectName,
  onChanged,
}: {
  inline?: boolean;
  taskIds: number[];
  tasks: Task[];
  layout: WorkspaceLayout;
  switchTasks: Task[];
  onLayoutChange: (layout: WorkspaceLayout) => void;
  onCloseTask: (id: number) => void;
  onCloseAll: () => void;
  onSwitchTask: (task: Task) => void;
  getProjectName?: (projectId: number) => string;
  onChanged: () => void;
}) {
  const isDesktop = useIsDesktop();
  const headerTop = useHeaderTop();
  // 哪个空格正在选任务（null=都收起）
  const [pickingSlot, setPickingSlot] = useState<number | null>(null);
  const openTasks = taskIds
    .map((id) => tasks.find((t) => t.id === id))
    .filter((t): t is Task => !!t);
  // 可加入的任务：可切换任务中排除已打开的
  const availableTasks = switchTasks.filter((t) => !taskIds.includes(t.id));

  // 固定抽屉模式下没有终端就不渲染；内嵌（tab 视图）模式即使为空也要显示空槽供选任务
  if (openTasks.length === 0 && !inline) return null;

  // 移动端：不并排，全屏显示最后激活的一个，可在面板内切换其余打开的任务
  if (!isDesktop && openTasks.length > 0) {
    const active = openTasks[openTasks.length - 1];
    return (
      <TerminalPanel
        key={`term-${active.id}`}
        task={active}
        switchTasks={switchTasks}
        onSwitchTask={onSwitchTask}
        getProjectName={getProjectName}
        onClose={onCloseAll}
        onChanged={onChanged}
      />
    );
  }

  const slots = Array.from({ length: layout }, (_, i) => openTasks[i] ?? null);
  const gridStyle: CSSProperties = {
    gridTemplateColumns: layout === 1 ? "1fr" : "1fr 1fr",
    gridTemplateRows: layout === 4 ? "1fr 1fr" : "1fr",
  };

  const body = (
      <div
        className={
          inline
            ? "flex h-full w-full flex-col overflow-hidden bg-slate-900"
            : `flex h-full w-full max-w-full flex-col overflow-hidden bg-slate-900 shadow-2xl ${DRAWER_WIDTH[layout]}`
        }
      >
        <div className="flex shrink-0 items-center gap-2 border-b border-slate-700 bg-slate-800 px-3 py-2">
          <span className="text-xs font-medium text-slate-200">终端工作台</span>
          <span className="text-[11px] text-dh-muted">{openTasks.length} 个终端</span>
          <div className="ml-auto flex items-center gap-1">
            {LAYOUT_META.map(({ value, icon, title }) => (
              <button
                key={value}
                type="button"
                className={`rounded-md px-2 py-1 text-xs font-medium ${
                  layout === value ? "bg-teal-600 text-white" : "text-slate-300 hover:bg-slate-700"
                }`}
                onClick={() => onLayoutChange(value)}
                title={title}
              >
                {icon} {value}
              </button>
            ))}
            <button
              type="button"
              className="ml-1 rounded-md px-2 py-1 text-xs text-slate-400 hover:bg-slate-700 hover:text-slate-200"
              onClick={onCloseAll}
              title="关闭全部终端"
            >
              ✕ 全部
            </button>
          </div>
        </div>
        <div className="grid min-h-0 flex-1 gap-1.5 bg-slate-900 p-1.5" style={gridStyle}>
          {slots.map((t, i) =>
            t ? (
              <div
                key={`cell-${t.id}`}
                className="min-h-0 overflow-hidden rounded-lg border border-slate-700 bg-dh-surface"
              >
                <TerminalPanel
                  embedded
                  task={t}
                  getProjectName={getProjectName}
                  onClose={() => onCloseTask(t.id)}
                  onChanged={onChanged}
                />
              </div>
            ) : pickingSlot === i ? (
              <div
                key={`pick-${i}`}
                className="flex min-h-0 flex-col overflow-hidden rounded-lg border border-slate-600 bg-slate-800"
              >
                <div className="flex shrink-0 items-center justify-between border-b border-slate-700 px-3 py-2">
                  <span className="text-xs font-medium text-slate-200">选择任务加入</span>
                  <button
                    type="button"
                    className="rounded px-1.5 py-0.5 text-xs text-slate-400 hover:bg-slate-700 hover:text-slate-200"
                    onClick={() => setPickingSlot(null)}
                    title="取消"
                  >
                    ✕
                  </button>
                </div>
                <div className="min-h-0 flex-1 overflow-y-auto p-1.5">
                  {availableTasks.length === 0 ? (
                    <div className="p-4 text-center text-xs text-dh-muted">没有其它可加入的任务</div>
                  ) : (
                    availableTasks.map((t) => {
                      const st = STATUS_STYLE[taskStatusStyleKey(t)] ?? STATUS_STYLE.draft;
                      return (
                        <button
                          key={t.id}
                          type="button"
                          className="flex w-full items-center gap-2 rounded-md px-2 py-2 text-left hover:bg-slate-700"
                          onClick={() => {
                            onSwitchTask(t);
                            setPickingSlot(null);
                          }}
                        >
                          <span className={`h-2 w-2 shrink-0 rounded-full ${st.dot}`} />
                          <span className="shrink-0 font-mono text-[11px] text-slate-400">#{t.id}</span>
                          <span className="min-w-0 flex-1 truncate text-xs text-slate-200">
                            {t.title || taskPromptText(t)}
                          </span>
                          {getProjectName && (
                            <span className="shrink-0 truncate text-[10px] text-dh-muted">
                              {getProjectName(t.project_id)}
                            </span>
                          )}
                        </button>
                      );
                    })
                  )}
                </div>
              </div>
            ) : (
              <button
                key={`empty-${i}`}
                type="button"
                className="flex min-h-0 items-center justify-center rounded-lg border border-dashed border-slate-700 bg-slate-800/40 px-3 text-center text-xs text-dh-muted hover:border-slate-600 hover:text-slate-400"
                onClick={() => setPickingSlot(i)}
              >
                ＋ 点击选择任务加入
              </button>
            ),
          )}
        </div>
      </div>
  );
  if (inline) return body;
  return (
    <div
      data-no-pull-refresh
      className="fixed inset-x-0 z-40 box-border flex justify-end bg-slate-900/20"
      style={{ top: `${headerTop}px`, bottom: 0 }}
    >
      {body}
    </div>
  );
}
