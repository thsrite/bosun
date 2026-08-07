import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  api,
  type ClaudeResource,
  type ClaudeResourceCategory,
  type ClaudeResourceContent,
} from "../api";
import { confirmDialog, promptDialog, toast } from "../overlay";

type Filter = ClaudeResourceCategory | "all";

const CATEGORY_ORDER: ClaudeResourceCategory[] = [
  "memory",
  "settings",
  "skill",
  "rule",
  "command",
  "agent",
  "hook",
  "file",
];

const CATEGORY_LABEL: Record<Filter, string> = {
  all: "全部",
  memory: "CLAUDE.md",
  settings: "设置",
  skill: "Skills",
  rule: "Rules",
  command: "Commands",
  agent: "Agents",
  hook: "Hooks",
  file: "文件",
};

function formatSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function formatTime(ts: number | null) {
  if (!ts) return "未创建";
  return new Date(ts * 1000).toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function categoryClass(category: ClaudeResourceCategory) {
  if (category === "memory") return "border-slate-400/40 bg-slate-400/10 text-dh-tsoft";
  if (category === "skill") return "border-sky-500/40 bg-sky-500/10 text-sky-300";
  if (category === "rule") return "border-amber-500/40 bg-amber-500/10 text-amber-400";
  if (category === "settings") return "border-violet-500/40 bg-violet-500/15 text-violet-300";
  return "border-dh-bsoft bg-dh-s2 text-dh-tsoft";
}

function parseError(err: unknown) {
  const raw = err instanceof Error ? err.message : String(err);
  try {
    return JSON.parse(raw).detail || raw;
  } catch {
    return raw;
  }
}

function isMarkdownResource(resource: ClaudeResource | null, selected: string) {
  const path = (resource?.path ?? selected).toLowerCase();
  return path.endsWith(".md") || path.endsWith(".mdc") || path.endsWith("/skill.md");
}

export function ClaudeManagerView() {
  const [root, setRoot] = useState("~/.claude");
  const [resources, setResources] = useState<ClaudeResource[]>([]);
  const [selected, setSelected] = useState("CLAUDE.md");
  const [doc, setDoc] = useState<ClaudeResourceContent | null>(null);
  const [content, setContent] = useState("");
  const [filter, setFilter] = useState<Filter>("all");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [editorOpen, setEditorOpen] = useState(false);
  const loadSeq = useRef(0);

  const dirty = !!doc && content !== doc.content;

  const refresh = useCallback(async () => {
    const data = await api.claude.list();
    setRoot(data.root);
    setResources(data.resources);
    return data.resources;
  }, []);

  const loadDoc = useCallback(async (path: string) => {
    const seq = loadSeq.current + 1;
    loadSeq.current = seq;
    setLoading(true);
    setDoc(null);
    setContent("");
    try {
      const loaded = await api.claude.get(path);
      if (seq !== loadSeq.current) return;
      setDoc(loaded);
      setContent(loaded.content);
    } catch (err) {
      if (seq !== loadSeq.current) return;
      toast(`读取失败：${parseError(err)}`, "error");
    } finally {
      if (seq === loadSeq.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh().catch((err) => toast(`加载 Claude 配置失败：${parseError(err)}`, "error"));
  }, [refresh]);

  useEffect(() => {
    loadDoc(selected);
  }, [loadDoc, selected]);

  const counts = useMemo(() => {
    const next: Record<Filter, number> = { all: resources.length } as Record<Filter, number>;
    for (const category of CATEGORY_ORDER) next[category] = 0;
    for (const item of resources) next[item.category] = (next[item.category] ?? 0) + 1;
    return next;
  }, [resources]);

  const groups = useMemo(() => {
    const visible = filter === "all" ? resources : resources.filter((item) => item.category === filter);
    return CATEGORY_ORDER.map((category) => ({
      category,
      items: visible.filter((item) => item.category === category),
    })).filter((group) => group.items.length > 0);
  }, [filter, resources]);

  async function choose(path: string) {
    if (path === selected) {
      setEditorOpen(true);
      return;
    }
    if (dirty && !(await confirmDialog("当前修改还没保存，切换后会丢失。继续？"))) return;
    setSelected(path);
    setEditorOpen(true);
  }

  async function save() {
    if (!doc || !dirty || saving) return;
    setSaving(true);
    try {
      const saved = await api.claude.save(doc.path, content);
      setDoc(saved);
      setContent(saved.content);
      await refresh();
      toast("已保存", "success");
    } catch (err) {
      toast(`保存失败：${parseError(err)}`, "error");
    } finally {
      setSaving(false);
    }
  }

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") {
        e.preventDefault();
        save();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  async function create(kind: "skill" | "rule" | "command" | "agent") {
    if (dirty && !(await confirmDialog("当前修改还没保存，新建后会切换文件。继续？"))) return;
    const label = kind === "skill" ? "Skill 名称" : `${CATEGORY_LABEL[kind]} 文件名`;
    const fallback =
      kind === "skill" ? "my-skill" : kind === "rule" ? "team/70-new-rule.md" : "new-item.md";
    const name = await promptDialog(label, fallback);
    if (!name?.trim()) return;
    try {
      const created = await api.claude.create(kind, name.trim());
      await refresh();
      setSelected(created.path);
      setDoc(created);
      setContent(created.content);
      setEditorOpen(true);
      toast("已创建", "success");
    } catch (err) {
      toast(`创建失败：${parseError(err)}`, "error");
    }
  }

  async function removeCurrent() {
    if (!doc?.deletable) return;
    const ok = await confirmDialog(`删除「${doc.label}」？\n\n${doc.disk_path}`, { danger: true });
    if (!ok) return;
    try {
      await api.claude.remove(doc.path);
      const next = await refresh();
      const fallback = next.find((item) => item.path === "CLAUDE.md") ?? next[0];
      setDoc(null);
      setContent("");
      setSelected(fallback?.path ?? "CLAUDE.md");
      setEditorOpen(false);
      toast("已删除", "success");
    } catch (err) {
      toast(`删除失败：${parseError(err)}`, "error");
    }
  }

  const active = doc ?? resources.find((item) => item.path === selected) ?? null;

  async function toggleResource(item: ClaudeResource, enabled: boolean) {
    if (!item.toggleable) return;
    if (dirty && item.path === doc?.path) {
      const ok = await confirmDialog("当前修改还没保存，切换开关后会丢失未保存内容。继续？");
      if (!ok) return;
    }
    try {
      const updated = await api.claude.setEnabled(item.path, enabled);
      await refresh();
      if (item.path === selected) {
        setDoc(updated);
        setContent(updated.content);
      }
      toast(enabled ? "已开启" : "已关闭", "success");
    } catch (err) {
      toast(`${enabled ? "开启" : "关闭"}失败：${parseError(err)}`, "error");
    }
  }

  async function closeEditor() {
    if (dirty && !(await confirmDialog("当前修改还没保存，关闭后会丢失。继续？"))) return;
    if (doc) setContent(doc.content);
    setEditorOpen(false);
  }

  return (
    <div className="flex min-h-full flex-col px-4 py-5 md:px-8 md:py-6">
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <div className="min-w-0 flex-1">
          <div className="text-sm font-semibold text-dh-text">Claude 全局配置</div>
          <div className="truncate text-xs text-slate-400" title={root}>
            {root}
          </div>
        </div>
        <div className="dh-scrollbar-none -mx-1 flex max-w-full gap-2 overflow-x-auto px-1 pb-1">
          <button
            className="shrink-0 rounded-lg bg-teal-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-teal-500"
            onClick={() => create("skill")}
          >
            + Skill
          </button>
          <button
            className="shrink-0 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-1.5 text-sm font-medium text-amber-400 hover:bg-amber-500/20"
            onClick={() => create("rule")}
          >
            + Rule
          </button>
          <button
            className="shrink-0 rounded-lg border border-dh-bsoft bg-dh-surface px-3 py-1.5 text-sm text-dh-tsoft hover:bg-dh-hover"
            onClick={() => create("command")}
          >
            + Command
          </button>
          <button
            className="shrink-0 rounded-lg border border-dh-bsoft bg-dh-surface px-3 py-1.5 text-sm text-dh-tsoft hover:bg-dh-hover"
            onClick={() => create("agent")}
          >
            + Agent
          </button>
        </div>
      </div>

      <div className="grid min-h-0 flex-1 grid-cols-1 gap-4 lg:grid-cols-[320px_minmax(0,1fr)]">
        <aside className="card flex min-h-[calc(100dvh-13rem)] flex-col overflow-hidden lg:min-h-[260px]">
          <div className="border-b border-dh-bsoft p-3">
            <div className="dh-scrollbar-none -mx-1 flex gap-1.5 overflow-x-auto px-1 pb-1 lg:flex-wrap lg:overflow-visible lg:pb-0">
              {(["all", ...CATEGORY_ORDER] as Filter[]).map((key) => {
                if (key !== "all" && !counts[key]) return null;
                const isActive = filter === key;
                return (
                  <button
                    key={key}
                    className={`shrink-0 rounded-md px-2 py-1 text-xs transition ${
                      isActive
                        ? "bg-black font-medium text-white ring-1 ring-slate-700"
                        : "border border-dh-bsoft text-dh-muted hover:bg-dh-hover"
                    }`}
                    onClick={() => setFilter(key)}
                  >
                    {CATEGORY_LABEL[key]} {counts[key] ?? 0}
                  </button>
                );
              })}
            </div>
          </div>

          <div className="min-h-0 flex-1 overflow-auto p-2">
            {groups.length === 0 ? (
              <div className="px-3 py-8 text-center text-xs text-slate-400">暂无文件</div>
            ) : (
              groups.map((group) => (
                <div key={group.category} className="mb-3 last:mb-0">
                  <div className="px-2 pb-1 text-[11px] font-semibold uppercase tracking-wide text-slate-400">
                    {CATEGORY_LABEL[group.category]}
                  </div>
                  <div className="space-y-1">
                    {group.items.map((item) => {
                      const isActive = item.path === selected;
                      return (
                        <div
                          key={item.path}
                          role="button"
                          tabIndex={0}
                          className={`w-full rounded-lg border px-2.5 py-2 text-left transition ${
                            isActive
                              ? "border-slate-400/40 bg-slate-400/10 ring-1 ring-teal-300"
                              : "border-transparent hover:border-dh-bsoft hover:bg-dh-hover"
                          }`}
                          onClick={() => choose(item.path)}
                          onKeyDown={(e) => {
                            if (e.key === "Enter" || e.key === " ") {
                              e.preventDefault();
                              choose(item.path);
                            }
                          }}
                        >
                          <div className="flex min-w-0 items-center gap-2">
                            <span className="min-w-0 flex-1 truncate text-sm font-medium text-dh-text">
                              {item.label}
                            </span>
                            {item.toggleable && (
                              <button
                                type="button"
                                className={`shrink-0 rounded-full border px-2 py-0.5 text-[10px] font-medium ${
                                  item.enabled
                                    ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-300"
                                    : "border-dh-border bg-dh-s2 text-dh-muted"
                                }`}
                                onClick={(e) => {
                                  e.stopPropagation();
                                  toggleResource(item, !item.enabled);
                                }}
                                title={item.enabled ? "关闭：移到隐藏禁用区，Claude 不再读取" : "开启：移回 Claude 全局目录"}
                              >
                                {item.enabled ? "开" : "关"}
                              </button>
                            )}
                            {!item.exists && (
                              <span className="shrink-0 rounded-full border border-dh-bsoft px-1.5 text-[10px] text-slate-400">
                                未创建
                              </span>
                            )}
                          </div>
                          <div className="mt-0.5 truncate text-[11px] text-slate-400">{item.path}</div>
                        </div>
                      );
                    })}
                  </div>
                </div>
              ))
            )}
          </div>
        </aside>

        <EditorPanel
          active={active}
          selected={selected}
          content={content}
          dirty={dirty}
          loading={loading}
          saving={saving}
          onContentChange={setContent}
          onSave={save}
          onDelete={removeCurrent}
          onToggle={(enabled) => active && toggleResource(active, enabled)}
          className="hidden min-h-[520px] lg:flex"
        />
      </div>

      {editorOpen && (
        <div
          className="fixed inset-0 z-50 flex bg-slate-950/70 p-0 backdrop-blur-sm lg:hidden"
          role="dialog"
          aria-modal="true"
        >
          <EditorPanel
            active={active}
            selected={selected}
            content={content}
            dirty={dirty}
            loading={loading}
            saving={saving}
            onContentChange={setContent}
            onSave={save}
            onDelete={removeCurrent}
            onToggle={(enabled) => active && toggleResource(active, enabled)}
            onClose={closeEditor}
            className="h-[100dvh] w-full rounded-none border-0"
            compact
          />
        </div>
      )}
    </div>
  );
}

function EditorPanel({
  active,
  selected,
  content,
  dirty,
  loading,
  saving,
  onContentChange,
  onSave,
  onDelete,
  onToggle,
  onClose,
  className = "",
  compact = false,
}: {
  active: ClaudeResource | null;
  selected: string;
  content: string;
  dirty: boolean;
  loading: boolean;
  saving: boolean;
  onContentChange: (value: string) => void;
  onSave: () => void;
  onDelete: () => void;
  onToggle: (enabled: boolean) => void;
  onClose?: () => void;
  className?: string;
  compact?: boolean;
}) {
  const [mode, setMode] = useState<"edit" | "preview">("preview");
  const markdown = isMarkdownResource(active, selected);

  useEffect(() => {
    setMode("preview");
  }, [selected]);

  return (
    <section className={`card flex flex-col overflow-hidden ${className}`}>
      <div className="flex flex-wrap items-center gap-2 border-b border-dh-bsoft p-3 md:gap-3 md:p-4">
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            {active && (
              <span className={`rounded-md border px-2 py-0.5 text-[11px] ${categoryClass(active.category)}`}>
                {CATEGORY_LABEL[active.category]}
              </span>
            )}
            <span className="min-w-0 truncate text-sm font-semibold text-dh-text">
              {active?.label ?? selected}
            </span>
            {dirty && <span className="text-xs font-medium text-amber-500">未保存</span>}
          </div>
          <div className="mt-1 truncate text-xs text-slate-400" title={active?.disk_path}>
            {active?.disk_path ?? ""}
          </div>
        </div>
        <div className="flex shrink-0 rounded-lg border border-dh-bsoft bg-slate-900/40 p-0.5">
          {(["edit", "preview"] as const).map((key) => (
            <button
              key={key}
              type="button"
              className={`rounded-md px-2.5 py-1 text-xs font-medium ${
                mode === key
                  ? "bg-dh-s2 text-dh-text"
                  : "text-slate-400 hover:bg-slate-800 hover:text-slate-100"
              }`}
              onClick={() => setMode(key)}
            >
              {key === "edit" ? "编辑" : "预览"}
            </button>
          ))}
        </div>
        <button
          className={`shrink-0 rounded-lg border px-3 py-1.5 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-40 ${
            active?.enabled
              ? "border-dh-bsoft bg-dh-surface text-dh-tsoft hover:bg-dh-hover"
              : "border-emerald-500/40 bg-emerald-500/10 text-emerald-300 hover:bg-emerald-500/20"
          }`}
          disabled={!active?.toggleable}
          onClick={() => active && onToggle(!active.enabled)}
          title={active?.enabled ? "关闭后 Claude 不再读取" : "开启后恢复到 Claude 全局目录"}
        >
          {active?.enabled ? "关闭" : "开启"}
        </button>
        <button
          className="shrink-0 rounded-lg bg-teal-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-teal-500 disabled:cursor-not-allowed disabled:opacity-50"
          disabled={!dirty || saving}
          onClick={onSave}
        >
          {saving ? "保存中" : "保存"}
        </button>
        <button
          className="shrink-0 rounded-lg border border-rose-500/40 bg-dh-surface px-3 py-1.5 text-sm text-rose-400 hover:bg-rose-500/20 disabled:cursor-not-allowed disabled:opacity-40"
          disabled={!active?.deletable}
          onClick={onDelete}
        >
          删除
        </button>
        {onClose && (
          <button
            className="grid h-9 w-9 shrink-0 place-items-center rounded-lg border border-dh-bsoft bg-dh-surface text-dh-muted hover:bg-dh-hover"
            onClick={onClose}
            aria-label="关闭"
            title="关闭"
          >
            ✕
          </button>
        )}
      </div>

      <div className={`flex min-h-0 flex-1 flex-col ${compact ? "p-3 pb-[max(0.75rem,env(safe-area-inset-bottom))]" : "p-4"}`}>
        {mode === "edit" ? (
          <textarea
            className={`flex-1 resize-none rounded-lg border border-dh-bsoft bg-dh-soft px-3 py-2 font-mono leading-6 text-dh-text focus:border-dh-m2 focus:outline-none disabled:opacity-50 ${
              compact ? "min-h-0 text-[16px]" : "min-h-[420px] text-[13px]"
            }`}
            spellCheck={false}
            value={content}
            disabled={loading}
            onChange={(e) => onContentChange(e.target.value)}
          />
        ) : (
          <div
            className={`min-h-0 flex-1 overflow-auto rounded-lg border border-dh-bsoft bg-dh-soft px-3 py-2 ${
              compact ? "text-[15px]" : "text-sm"
            }`}
            onClick={() => setMode("edit")}
          >
            {loading ? (
              <div className="text-sm text-slate-400">读取中</div>
            ) : markdown ? (
              <div className="chat-md">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{content || " "}</ReactMarkdown>
              </div>
            ) : (
              <pre className="whitespace-pre-wrap break-words font-mono text-[13px] leading-6 text-slate-200">
                {content}
              </pre>
            )}
          </div>
        )}
        <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-400">
          <span>{loading ? "读取中" : active?.exists ? formatSize(active.size) : "新文件"}</span>
          <span>{formatTime(active?.updated_at ?? null)}</span>
          <span className="min-w-0 truncate">{active?.path ?? selected}</span>
        </div>
      </div>
    </section>
  );
}
