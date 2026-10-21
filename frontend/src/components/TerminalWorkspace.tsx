import { useEffect, useState, type CSSProperties, type ReactNode } from "react";
import {
  DndContext,
  DragOverlay,
  PointerSensor,
  useDraggable,
  useDroppable,
  useSensor,
  useSensors,
  type DragEndEvent,
  type DragStartEvent,
} from "@dnd-kit/core";
import { STATUS_STYLE, taskStatusStyleKey } from "../theme";
import type { Task } from "../types";
import { TerminalPanel } from "./TerminalPanel";
import { taskPromptText } from "../taskText";
import { moveToSlot } from "../workspaceSlots";

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

/**
 * 有任务的格子：整格是放置目标，顶栏把手是拖动源（只拖把手，不拖整块终端）。
 * 放大时内层改为绝对定位盖满网格（网格容器是定位祖先），外层留在原位占住格子，
 * 其余格子 invisible——尺寸都不变，终端不会因此重排或把 PTY 压小。
 */
function TaskCell({
  task,
  slot,
  style,
  maximized,
  hidden,
  canDrag,
  children,
}: {
  task: Task;
  slot: number;
  style?: CSSProperties;
  maximized: boolean;
  hidden: boolean;
  canDrag: boolean;
  children: (accessory: ReactNode) => ReactNode;
}) {
  const { setNodeRef: setDropRef, isOver } = useDroppable({ id: `slot-${slot}` });
  const { setNodeRef: setDragRef, attributes, listeners, isDragging } = useDraggable({
    id: `task-${task.id}`,
    disabled: !canDrag,
  });
  const handle = canDrag ? (
    <button
      ref={setDragRef}
      type="button"
      className="cursor-grab touch-none whitespace-nowrap rounded-lg border border-dh-bsoft px-2 py-1 text-xs text-dh-tsoft hover:bg-dh-hover active:cursor-grabbing"
      title="拖到另一格交换位置"
      {...attributes}
      {...listeners}
    >
      ⠿
    </button>
  ) : null;
  return (
    <div ref={setDropRef} className={`min-h-0 ${hidden ? "invisible" : ""}`} style={style}>
      <div
        className={`overflow-hidden rounded-lg border bg-dh-surface ${
          maximized ? "absolute inset-1.5 z-10" : "h-full w-full"
        } ${
          isOver && !isDragging
            ? "border-dh-accent ring-2 ring-dh-accent/50"
            : maximized
              ? "border-dh-accent"
              : "border-dh-border"
        } ${isDragging ? "opacity-60" : ""}`}
      >
        {children(handle)}
      </div>
    </div>
  );
}

function EmptySlot({
  slot,
  style,
  hidden,
  children,
}: {
  slot: number;
  style?: CSSProperties;
  hidden: boolean;
  children: ReactNode;
}) {
  const { setNodeRef, isOver } = useDroppable({ id: `slot-${slot}` });
  return (
    <div
      ref={setNodeRef}
      className={`min-h-0 rounded-lg ${hidden ? "invisible" : ""} ${isOver ? "ring-2 ring-dh-accent/50" : ""}`}
      style={style}
    >
      {children}
    </div>
  );
}

/** 多终端并排宿主：桌面端多种宫格布局，格子可放大、可拖动交换；移动端只显示激活的一个。 */
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
  onReorderTasks,
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
  /** 拖动交换格子后的新顺序（顺序即格子位置） */
  onReorderTasks: (taskIds: number[]) => void;
  getProjectName?: (projectId: number) => string;
  onChanged: () => void;
}) {
  const isDesktop = useIsDesktop();
  const headerTop = useHeaderTop();
  // 哪个空格正在选任务（null=都收起）
  const [pickingSlot, setPickingSlot] = useState<number | null>(null);
  // 放大的格子：盖满整个工作台，其余格子保持尺寸只是隐藏（终端不重排、PTY 不被压小）
  const [maximizedId, setMaximizedId] = useState<number | null>(null);
  const [draggingId, setDraggingId] = useState<number | null>(null);
  // 拖出 6px 才算拖动，避免点把手时误触发
  const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 6 } }));
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
  // 放大的任务已被关掉/移出时自动还原
  const maximized = maximizedId !== null && slots.some((t) => t?.id === maximizedId) ? maximizedId : null;
  const draggingTask = draggingId === null ? null : openTasks.find((t) => t.id === draggingId) ?? null;

  function onDragStart(event: DragStartEvent) {
    setDraggingId(Number(String(event.active.id).replace("task-", "")));
  }

  function onDragEnd(event: DragEndEvent) {
    setDraggingId(null);
    if (!event.over) return;
    const fromId = Number(String(event.active.id).replace("task-", ""));
    const toSlot = Number(String(event.over.id).replace("slot-", ""));
    const next = moveToSlot(taskIds, fromId, toSlot);
    if (next) onReorderTasks(next);
  }

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
        <DndContext
          sensors={sensors}
          onDragStart={onDragStart}
          onDragEnd={onDragEnd}
          onDragCancel={() => setDraggingId(null)}
        >
        <div className="relative grid min-h-0 flex-1 gap-1.5 bg-dh-soft p-1.5" style={gridStyle}>
          {slots.map((t, i) =>
            t ? (
              <TaskCell
                key={`cell-${t.id}`}
                task={t}
                slot={i}
                style={cellStyle(i)}
                maximized={maximized === t.id}
                hidden={maximized !== null && maximized !== t.id}
                canDrag={openTasks.length > 1 && maximized === null}
              >
                {(accessory) => (
                  <TerminalPanel
                    embedded
                    task={t}
                    getProjectName={getProjectName}
                    onClose={() => onCloseTask(t.id)}
                    onChanged={onChanged}
                    headerAccessory={
                      <>
                        {accessory}
                        {spec.slots > 1 && (
                          <button
                            type="button"
                            className="whitespace-nowrap rounded-lg border border-dh-bsoft px-2.5 py-1 text-xs text-dh-tsoft hover:bg-dh-hover"
                            onClick={() => setMaximizedId(maximized === t.id ? null : t.id)}
                            title={maximized === t.id ? "还原到格子" : "放大这一格（其余终端保持连接）"}
                          >
                            {maximized === t.id ? "⤡ 还原" : "⤢ 放大"}
                          </button>
                        )}
                      </>
                    }
                  />
                )}
              </TaskCell>
            ) : pickingSlot === i ? (
              <div
                key={`pick-${i}`}
                className={`flex min-h-0 flex-col overflow-hidden rounded-lg border border-dh-border bg-dh-s2 ${
                  maximized !== null ? "invisible" : ""
                }`}
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
              <EmptySlot key={`empty-${i}`} slot={i} style={cellStyle(i)} hidden={maximized !== null}>
                <button
                  type="button"
                  className="flex h-full w-full items-center justify-center rounded-lg border border-dashed border-dh-border bg-dh-s2/40 px-3 text-center text-xs text-dh-muted hover:border-dh-border hover:text-slate-400"
                  onClick={() => setPickingSlot(i)}
                >
                  {draggingId !== null ? "松开放到这里" : "＋ 点击选择任务加入"}
                </button>
              </EmptySlot>
            ),
          )}
        </div>
        <DragOverlay dropAnimation={null}>
          {draggingTask && (
            <div className="max-w-[280px] truncate rounded-lg border border-dh-accent bg-dh-surface px-3 py-2 text-xs text-dh-text shadow-xl">
              <span className="font-mono text-dh-muted">#{draggingTask.id}</span>{" "}
              {draggingTask.title || taskPromptText(draggingTask)}
            </div>
          )}
        </DragOverlay>
        </DndContext>
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
