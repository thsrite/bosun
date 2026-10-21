import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { STATUS_STYLE, taskStatusStyleKey } from "../theme";
import type { Task } from "../types";
import { taskPromptText } from "../taskText";
import { URGENCY_GROUP_LABEL, groupForSwitcher } from "../taskUrgency";
import { isCoarsePointer } from "../pointer";
import { Modal } from "./Modal";

/** 快速切换面板：搜索 + 按紧急度分组，点一下（或回车）跳到对应任务。 */
export function TaskQuickSwitcher({
  tasks,
  currentId,
  getProjectName,
  onPick,
  onClose,
}: {
  tasks: Task[];
  currentId: number;
  getProjectName?: (projectId: number) => string;
  onPick: (task: Task) => void;
  onClose: () => void;
}) {
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const projectName = (t: Task) => getProjectName?.(t.project_id) ?? "";

  const groups = useMemo(
    () => groupForSwitcher(tasks, query, projectName),
    [tasks, query, getProjectName],
  );
  const flat = useMemo(() => groups.flatMap((g) => g.tasks), [groups]);

  useEffect(() => {
    // 触屏设备不自动聚焦：一打开就弹软键盘会挡住大半个列表；
    // 终端输入框若仍持有焦点（键盘开着），顺手释放掉让键盘收起
    if (!isCoarsePointer()) inputRef.current?.focus();
    else if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
  }, []);

  // 只在打开和改搜索词时重置高亮：任务列表每几秒刷新一次，跟着重置会打断键盘选择。
  // 重置到第一条非当前任务（选当前任务没有意义）。
  useEffect(() => {
    const first = flat.findIndex((t) => t.id !== currentId);
    setActive(first < 0 ? 0 : first);
  }, [query]);
  const activeIndex = Math.min(active, Math.max(0, flat.length - 1));

  useEffect(() => {
    listRef.current
      ?.querySelector<HTMLElement>(`[data-index="${activeIndex}"]`)
      ?.scrollIntoView({ block: "nearest" });
  }, [activeIndex]);

  function pick(task: Task | undefined) {
    if (!task) return;
    onClose();
    if (task.id !== currentId) onPick(task);
  }

  function onKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (flat.length === 0) return;
      const delta = e.key === "ArrowDown" ? 1 : -1;
      setActive(() => (activeIndex + delta + flat.length) % flat.length);
    } else if (e.key === "Enter") {
      e.preventDefault();
      pick(flat[activeIndex]);
    } else if (e.key === "Escape") {
      e.preventDefault();
      onClose();
    }
  }

  let index = -1;
  return (
    <Modal title="切换任务" onClose={onClose}>
      <input
        ref={inputRef}
        type="search"
        enterKeyHint="go"
        className="mb-3 w-full rounded-lg border border-dh-bsoft bg-dh-surface px-3 py-2 text-sm text-dh-text"
        placeholder={isCoarsePointer() ? "搜索编号、标题、项目…" : "搜索编号、标题、项目、引擎…（↑↓ 选择，回车跳转）"}
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        onKeyDown={onKeyDown}
      />
      <div ref={listRef} className="max-h-[60vh] space-y-3 overflow-y-auto">
        {groups.length === 0 && <div className="py-6 text-center text-sm text-dh-muted">没有匹配的任务</div>}
        {groups.map(({ group, tasks: items }) => (
          <div key={group}>
            <div className="mb-1 flex items-center gap-2 px-1 text-xs font-medium text-dh-muted">
              {URGENCY_GROUP_LABEL[group]}
              <span className="tabular-nums">{items.length}</span>
            </div>
            <div className="space-y-1">
              {items.map((t) => {
                index += 1;
                const i = index;
                const st = STATUS_STYLE[taskStatusStyleKey(t)] ?? STATUS_STYLE.draft;
                const isCurrent = t.id === currentId;
                return (
                  <button
                    key={t.id}
                    type="button"
                    data-index={i}
                    className={`flex w-full items-start gap-2 rounded-lg border px-2.5 py-2 text-left ${
                      i === activeIndex ? "border-dh-accent bg-dh-hover" : "border-dh-bsoft hover:bg-dh-hover"
                    }`}
                    onMouseMove={() => setActive(i)}
                    onClick={() => pick(t)}
                  >
                    <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${st.dot}`} />
                    <span className="min-w-0 flex-1">
                      <span className="flex items-center gap-1.5 text-xs text-dh-muted">
                        <span className="font-mono">#{t.id}</span>
                        <span className="truncate">{projectName(t)}</span>
                        <span className={`shrink-0 font-medium ${st.text}`}>{st.label}</span>
                        {isCurrent && <span className="shrink-0 text-dh-tsoft">· 当前</span>}
                      </span>
                      <span className="mt-0.5 block truncate text-sm font-medium text-dh-text">
                        {t.title || taskPromptText(t)}
                      </span>
                      {t.report_summary && (
                        <span className="mt-0.5 block truncate text-xs text-dh-muted" title={t.report_summary}>
                          {t.report_summary}
                        </span>
                      )}
                    </span>
                  </button>
                );
              })}
            </div>
          </div>
        ))}
      </div>
    </Modal>
  );
}
