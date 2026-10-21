import { useEffect, useState, type CSSProperties } from "react";
import { STATUS_STYLE, taskStatusStyleKey } from "../theme";
import type { Task } from "../types";
import { TerminalPanel } from "./TerminalPanel";
import { taskPromptText } from "../taskText";

export type WorkspaceLayout = "1" | "2" | "3" | "3m" | "4" | "6";

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

type LayoutSpec = {
  /** 能容纳的终端数 */
  slots: number;
  icon: string;
  title: string;
  columns: string;
  rows: string;
  /** 格子是否占多行，下标对应 slot */
  spans?: Record<number, string>;
  // 布局越多，抽屉越宽，给每个终端留出可读宽度
  drawer: string;
};

export const LAYOUTS: Record<WorkspaceLayout, LayoutSpec> = {
  "1": { slots: 1, icon: "▢", title: "单格", columns: "1fr", rows: "1fr", drawer: "lg:w-[48%] lg:min-w-[560px]" },
  "2": { slots: 2, icon: "◫", title: "左右两格", columns: "1fr 1fr", rows: "1fr", drawer: "lg:w-[74%]" },
  "3": { slots: 3, icon: "▥", title: "三列", columns: "1fr 1fr 1fr", rows: "1fr", drawer: "lg:w-[92%]" },
  "3m": {
    slots: 3,
    icon: "◧",
    title: "左大右二",
    columns: "3fr 2fr",
    rows: "1fr 1fr",
    spans: { 0: "1 / span 2" },
    drawer: "lg:w-[92%]",
  },
  "4": { slots: 4, icon: "⊞", title: "四宫格", columns: "1fr 1fr", rows: "1fr 1fr", drawer: "lg:w-[92%]" },
  "6": { slots: 6, icon: "▦", title: "六宫格（3×2）", columns: "1fr 1fr 1fr", rows: "1fr 1fr", drawer: "lg:w-[96%]" },
};

const LAYOUT_ORDER: WorkspaceLayout[] = ["1", "2", "3", "3m", "4", "6"];

export const MAX_WORKSPACE_SLOTS = 6;

export function isWorkspaceLayout(v: unknown): v is WorkspaceLayout {
  return typeof v === "string" && v in LAYOUTS;
}

/** 旧版存的是数字 1/2/4，读出时转成新 id */
export function parseWorkspaceLayout(v: unknown): WorkspaceLayout {
  const key = typeof v === "number" ? String(v) : v;
  return isWorkspaceLayout(key) ? key : "1";
}

/** 打开 N 个终端时，选能容纳的最小布局：1→单格 2→左右 3→三列 4→四宫格 5~6→六宫格 */
export function fitLayout(count: number): WorkspaceLayout {
  return count <= 1 ? "1" : count <= 2 ? "2" : count <= 3 ? "3" : count <= 4 ? "4" : "6";
}

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

  const spec = LAYOUTS[layout];
  const slots = Array.from({ length: spec.slots }, (_, i) => openTasks[i] ?? null);
  const gridStyle: CSSProperties = {
    gridTemplateColumns: spec.columns,
    gridTemplateRows: spec.rows,
  };
  const cellStyle = (i: number): CSSProperties | undefined =>
    spec.spans?.[i] ? { gridRow: spec.spans[i] } : undefined;

  const body = (
      <div
        className={
          inline
            ? "flex h-full w-full flex-col overflow-hidden bg-dh-soft"
            : `flex h-full w-full max-w-full flex-col overflow-hidden bg-dh-soft shadow-2xl ${spec.drawer}`
        }
      >
        <div className="flex shrink-0 items-center gap-2 border-b border-dh-border bg-dh-s2 px-3 py-2">
          <span className="text-xs font-medium text-slate-200">终端工作台</span>
          <span className="text-[11px] text-dh-muted">{openTasks.length} 个终端</span>
          <div className="ml-auto flex items-center gap-1">
            {LAYOUT_ORDER.map((value) => (
              <button
                key={value}
                type="button"
                className={`rounded-md px-2 py-1 text-xs font-medium ${
                  layout === value ? "bg-dh-accent text-dh-accfg" : "text-slate-300 hover:bg-dh-hover"
                }`}
                onClick={() => onLayoutChange(value)}
                title={LAYOUTS[value].title}
              >
                {LAYOUTS[value].icon} {LAYOUTS[value].slots}
              </button>
            ))}
            <button
              type="button"
              className="ml-1 rounded-md px-2 py-1 text-xs text-slate-400 hover:bg-dh-hover hover:text-slate-200"
              onClick={onCloseAll}
              title="关闭全部终端"
            >
              ✕ 全部
            </button>
          </div>
        </div>
        <div className="grid min-h-0 flex-1 gap-1.5 bg-dh-soft p-1.5" style={gridStyle}>
          {slots.map((t, i) =>
            t ? (
              <div
                key={`cell-${t.id}`}
                className="min-h-0 overflow-hidden rounded-lg border border-dh-border bg-dh-surface"
                style={cellStyle(i)}
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
                className="flex min-h-0 flex-col overflow-hidden rounded-lg border border-dh-border bg-dh-s2"
                style={cellStyle(i)}
              >
                <div className="flex shrink-0 items-center justify-between border-b border-dh-border px-3 py-2">
                  <span className="text-xs font-medium text-slate-200">选择任务加入</span>
                  <button
                    type="button"
                    className="rounded px-1.5 py-0.5 text-xs text-slate-400 hover:bg-dh-hover hover:text-slate-200"
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
                          className="flex w-full items-center gap-2 rounded-md px-2 py-2 text-left hover:bg-dh-hover"
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
                style={cellStyle(i)}
                className="flex min-h-0 items-center justify-center rounded-lg border border-dashed border-dh-border bg-dh-s2/40 px-3 text-center text-xs text-dh-muted hover:border-dh-border hover:text-slate-400"
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
      className="fixed inset-x-0 z-40 box-border flex justify-end bg-black/20"
      style={{ top: `${headerTop}px`, bottom: 0 }}
    >
      {body}
    </div>
  );
}
