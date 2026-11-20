import { useEffect, useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api } from "../api";
import { confirmDialog, toast } from "../overlay";
import type {
  OrchestrationRun,
  ReceiptFileItem,
  RunTrace,
  Task,
  WorkflowGate,
  WorkflowNode,
  WorkflowPolicy,
} from "../types";
import { WorkflowCanvas, type CanvasSelection } from "./WorkflowCanvas";

const labels: Record<string, string> = {
  draft: "待启动", queued: "已排队", running: "执行中", done: "已完成", interrupted: "已中断", offline: "已掉线",
  failed: "失败", cancelled: "已取消", waiting_input: "等待输入", needs_input: "需要补充", rework: "退回返工",
  rejected: "交接被拦截", passed: "通过", error: "未完成验证", handoff: "交接", ask: "提问", answer: "答复", system: "系统记录",
  approved: "产品已确认", rejected_impl: "实现不符 · 退回开发", rejected_spec: "口径变更 · 退回需求",
};
export const statusText = (state: string) => labels[state] || state;
const live = (state: string) => ["running", "queued", "waiting_input"].includes(state);
const when = (n: number | null | undefined) => n ? new Date(n * 1000).toLocaleString("zh-CN", { hour12: false }) : "—";

const TRUST_LEVEL_OPTIONS = [
  { level: 0, label: "L0 · 影子观察", desc: "仅影子推演，所有动作由人执行" },
  { level: 1, label: "L1 · 协助开发", desc: "人定任务与合并，AI 隔离开发自测" },
  { level: 2, label: "L2 · 自动提审", desc: "就绪单自动派工复测提审，人定合并" },
  { level: 3, label: "L3 · 有限自动合并", desc: "白名单内低风险改动自动入队合并" },
  { level: 4, label: "L4 · 授权域自治", desc: "授权域内按预设样例自动签收合并" },
];

async function downloadEvidence(runId: number, receipt: number) {
  try {
    const url = URL.createObjectURL(await api.deliveryArchive(runId, receipt));
    const link = document.createElement("a");
    link.href = url;
    link.download = `回执-${receipt}.zip`;
    link.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (error) {
    toast(`下载证据失败：${String(error)}`, "error");
  }
}

function gateStyle(status: WorkflowGate["status"]): { card: string; icon: string; badge: string; text: string } {
  if (status === "passed") {
    return {
      card: "border-dh-bsoft border-l-4 border-l-emerald-500 bg-dh-surface",
      icon: "✓",
      badge: "bg-emerald-500/15 text-emerald-400",
      text: "通过",
    };
  }
  if (status === "failed") {
    return {
      card: "border-dh-bsoft border-l-4 border-l-rose-500 bg-dh-surface",
      icon: "✕",
      badge: "bg-rose-500/15 text-rose-400",
      text: "未通过",
    };
  }
  if (status === "running" || status === "waiting") {
    return {
      card: "border-dh-bsoft border-l-4 border-l-amber-400 bg-dh-surface",
      icon: "◔",
      badge: "bg-amber-500/15 text-amber-400",
      text: status === "running" ? "执行中" : "待确认",
    };
  }
  return {
    card: "border-dh-bsoft border-l-4 border-l-slate-600 bg-dh-surface/80",
    icon: "–",
    badge: "bg-dh-s2 text-dh-muted",
    text: status === "skipped" ? "已跳过" : "待执行",
  };
}

/** 解析阶段产物：自动拆出 bosun-acceptance 结构化协议与正文 Markdown，彻底告别一坨黑框文本。 */
function parseArtifact(raw?: string | null): { body: string; acceptance: Record<string, any> | null } {
  const text = (raw || "").trim();
  if (!text) return { body: "", acceptance: null };
  const match = text.match(/```bosun-acceptance[ \t]*\r?\n([\s\S]*?)```/);
  if (!match) return { body: text, acceptance: null };
  let parsed: Record<string, any> | null = null;
  try {
    const obj = JSON.parse(match[1]);
    if (obj && typeof obj === "object") parsed = obj as Record<string, any>;
  } catch {
    parsed = null;
  }
  const body = (text.slice(0, match.index) + text.slice((match.index ?? 0) + match[0].length)).trim();
  return { body, acceptance: parsed };
}

function ArtifactViewer({ title, value, defaultOpen = false }: { title: string; value?: string | null; defaultOpen?: boolean }) {
  const { body, acceptance } = useMemo(() => parseArtifact(value), [value]);
  const items = Array.isArray(acceptance?.items) ? (acceptance.items as Record<string, any>[]) : [];

  return (
    <div className="rounded-xl border border-dh-bsoft bg-dh-surface p-4 space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-dh-bsoft/70 pb-2.5">
        <h4 className="text-sm font-semibold text-dh-text">{title}</h4>
        {!value?.trim() && <span className="text-xs text-dh-muted">暂无内容</span>}
      </div>

      {/* 结构化协议摘要（如有 bosun-acceptance 区块） */}
      {acceptance && (
        <div className="space-y-3 rounded-xl border border-dh-accent/25 bg-dh-soft/70 p-3.5">
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <span className="font-semibold text-dh-accent">结构化交付协议</span>
            {acceptance.kind && (
              <span className="rounded-md bg-dh-surface px-2 py-0.5 font-medium text-dh-text">
                类型：{String(acceptance.kind)}
              </span>
            )}
            {acceptance.delivery && (
              <span className="rounded-md bg-dh-surface px-2 py-0.5 text-dh-tsoft">
                目标：{acceptance.delivery === "merge" ? "合并入主干" : "提交待审"}
              </span>
            )}
            {acceptance.baseline && (
              <span className="rounded-md bg-dh-surface px-2 py-0.5 font-mono text-[11px] text-dh-muted" title={String(acceptance.baseline)}>
                基线 {String(acceptance.baseline).slice(0, 10)}
              </span>
            )}
            {acceptance.version && (
              <span className="rounded-md bg-dh-surface px-2 py-0.5 font-mono text-[11px] text-emerald-400" title={String(acceptance.version)}>
                候选 {String(acceptance.version).slice(0, 10)}
              </span>
            )}
          </div>

          {acceptance.self_test && (
            <div className="rounded-lg bg-dh-surface p-2.5 text-[13px] leading-relaxed text-dh-tsoft">
              <span className="font-semibold text-dh-text">自测结论：</span>
              {String(acceptance.self_test)}
            </div>
          )}

          {items.length > 0 && (
            <div className="space-y-2">
              <div className="text-xs font-medium text-dh-muted">必验项清单（{items.length} 项）</div>
              <div className="grid gap-2 md:grid-cols-2">
                {items.map((it, i) => (
                  <div key={String(it.id ?? i)} className="rounded-lg border border-dh-bsoft bg-dh-surface p-3 space-y-1.5 text-[13px]">
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-semibold text-dh-text">{String(it.id ?? `#${i + 1}`)}</span>
                      <div className="flex items-center gap-1.5">
                        {it.area && <span className="rounded bg-dh-s2 px-1.5 py-0.5 text-[10px] uppercase text-dh-muted">{String(it.area)}</span>}
                        {it.status && (
                          <span className={`rounded px-1.5 py-0.5 text-[11px] font-medium ${
                            it.status === "通过" ? "bg-emerald-500/15 text-emerald-400"
                              : it.status === "失败" ? "bg-rose-500/15 text-rose-400"
                              : "bg-amber-500/15 text-amber-400"
                          }`}>
                            {String(it.status)}
                          </span>
                        )}
                      </div>
                    </div>
                    {it.input && <p className="text-xs leading-relaxed text-dh-muted"><b className="text-dh-tsoft">输入：</b>{String(it.input)}</p>}
                    {it.expected && <p className="text-xs leading-relaxed text-dh-tsoft"><b className="text-dh-text">预期：</b>{String(it.expected)}</p>}
                    {it.actual && <p className="text-xs leading-relaxed text-dh-tsoft"><b className="text-dh-text">实际：</b>{String(it.actual)}</p>}
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {/* 正文说明（Markdown 渲染，清晰有层次） */}
      {body && (
        <div className="prose prose-invert prose-sm max-w-none text-[13px] leading-relaxed text-dh-tsoft [&_code]:rounded [&_code]:bg-dh-soft [&_code]:px-1 [&_code]:py-0.5 [&_pre]:overflow-x-auto [&_pre]:rounded-lg [&_pre]:bg-dh-soft [&_pre]:p-3 [&_h1]:text-base [&_h2]:text-sm [&_h3]:text-sm [&_ul]:list-disc [&_ul]:pl-5 [&_ol]:list-decimal [&_ol]:pl-5">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{body}</ReactMarkdown>
        </div>
      )}

      {/* 原始文本折叠，需要核对精确字符时展开 */}
      {value?.trim() && (
        <details open={defaultOpen && !acceptance && !body} className="rounded-lg border border-dh-bsoft/70 bg-dh-soft/50 px-3 py-2 text-xs">
          <summary className="cursor-pointer text-dh-muted hover:text-dh-text">查看原始产物文本</summary>
          <pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap break-words font-mono text-[11px] leading-relaxed text-dh-tsoft">
            {value}
          </pre>
        </details>
      )}
    </div>
  );
}

function ReceiptEvidencePreview({ runId, receiptId }: { runId: number; receiptId: number }) {
  const [open, setOpen] = useState(false);
  const [files, setFiles] = useState<ReceiptFileItem[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [activeText, setActiveText] = useState<{ name: string; content: string } | null>(null);

  async function toggle() {
    if (open) {
      setOpen(false);
      return;
    }
    setOpen(true);
    if (files !== null) return;
    setLoading(true);
    try {
      const res = await api.receiptFiles(runId, receiptId);
      setFiles(res.files);
    } catch (err) {
      toast(`读取证据列表失败：${String(err)}`, "error");
    } finally {
      setLoading(false);
    }
  }

  async function openTextFile(name: string) {
    try {
      const res = await fetch(api.receiptFileUrl(runId, receiptId, name));
      if (!res.ok) throw new Error(await res.text());
      const content = await res.text();
      setActiveText({ name, content: content.slice(0, 40000) });
    } catch (err) {
      toast(`预览文件失败：${String(err)}`, "error");
    }
  }

  const images = (files ?? []).filter((f) => f.kind === "image");
  const texts = (files ?? []).filter((f) => f.kind !== "image");

  return (
    <div className="mt-3 space-y-2.5">
      <div className="flex flex-wrap items-center gap-3 text-xs">
        <button
          type="button"
          onClick={() => void toggle()}
          className="rounded-md border border-sky-500/40 bg-sky-500/10 px-2.5 py-1 font-medium text-sky-400 hover:bg-sky-500/20"
        >
          {open ? "收起在线预览" : "在线预览截图与报告"}
        </button>
        <button
          type="button"
          onClick={() => void downloadEvidence(runId, receiptId)}
          className="rounded-md border border-dh-bsoft bg-dh-soft px-2.5 py-1 text-dh-tsoft hover:text-dh-text"
        >
          下载完整证据 (.zip)
        </button>
      </div>
      {open && (
        <div className="space-y-3 rounded-xl border border-dh-bsoft bg-dh-soft p-3">
          {loading && <p className="text-xs text-dh-muted">正在解析证据包…</p>}
          {!loading && files && files.length === 0 && <p className="text-xs text-dh-muted">证据包为空。</p>}
          {images.length > 0 && (
            <div className="grid gap-3 md:grid-cols-2">
              {images.map((img) => (
                <figure key={img.name} className="overflow-hidden rounded-lg border border-dh-bsoft bg-dh-surface p-2.5">
                  <figcaption className="mb-2 flex items-center justify-between text-xs font-medium text-dh-text">
                    <span className="truncate">{img.name}</span>
                    <span className="text-dh-muted">{Math.max(1, Math.round(img.size / 1024))} KB</span>
                  </figcaption>
                  <img
                    src={api.receiptFileUrl(runId, receiptId, img.name)}
                    alt={img.name}
                    loading="lazy"
                    className="max-h-80 w-full rounded border border-dh-bsoft bg-black/5 object-contain"
                  />
                </figure>
              ))}
            </div>
          )}
          {texts.length > 0 && (
            <div className="flex flex-wrap gap-2">
              {texts.map((f) => (
                <button
                  key={f.name}
                  type="button"
                  onClick={() => void openTextFile(f.name)}
                  className={`rounded-md border px-2.5 py-1 text-xs ${
                    activeText?.name === f.name
                      ? "border-dh-accent bg-dh-accent/15 text-dh-text"
                      : "border-dh-bsoft bg-dh-surface text-dh-tsoft hover:border-dh-accent"
                  }`}
                >
                  {f.name}
                </button>
              ))}
            </div>
          )}
          {activeText && (
            <div className="rounded-lg border border-dh-bsoft bg-dh-surface p-3">
              <div className="mb-1.5 flex items-center justify-between text-xs font-medium text-dh-text">
                <span>{activeText.name}</span>
                <button type="button" onClick={() => setActiveText(null)} className="text-dh-muted underline">关闭</button>
              </div>
              <pre className="max-h-72 overflow-auto whitespace-pre-wrap break-words font-mono text-xs leading-relaxed">
                {activeText.content}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

type Plan = { steps?: { position: number; name: string; role_prompt: string }[]; acceptance_gate?: boolean; remote_checks?: boolean; specialist_checks?: boolean };
function planFor(item: OrchestrationRun): Plan {
  try { return JSON.parse(item.definition_snapshot); } catch { return {}; }
}

type InspectorTab = "gates" | "io" | "evidence" | "timeline" | "signoff";

export function OrchestrationTrace({
  initial,
  runId,
  onOpenTask,
}: {
  initial?: OrchestrationRun;
  runId?: number;
  onOpenTask: (t: Task) => void;
}) {
  const targetRunId = initial?.id ?? runId ?? 0;
  const [trace, setTrace] = useState<RunTrace | null>(null);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [selection, setSelection] = useState<CanvasSelection | null>(null);
  const [tab, setTab] = useState<InspectorTab>("gates");
  const [synced, setSynced] = useState<number | null>(null);

  // 信任策略编辑面板
  const [policyOpen, setPolicyOpen] = useState(false);
  const [policySaving, setPolicySaving] = useState(false);
  const [draftTrust, setDraftTrust] = useState(1);
  const [draftDiffGate, setDraftDiffGate] = useState(true);
  const [draftSignoff, setDraftSignoff] = useState(true);
  const [draftGlobs, setDraftGlobs] = useState("");

  // 产品验收卡签收表单
  const [signer, setSigner] = useState("产品负责人");
  const [signoffComment, setSignoffComment] = useState("");
  const [signoffBusy, setSignoffBusy] = useState(false);

  useEffect(() => {
    if (!targetRunId) return;
    let gone = false;
    let timer: number;
    async function load() {
      let again = true;
      try {
        if (!document.hidden) {
          const data = await api.runTrace(targetRunId);
          if (gone) return;
          setTrace(data);
          setError("");
          setSynced(Date.now() / 1000);
          again = live(data.run.status);
        }
      } catch (e) {
        if (!gone) setError(String(e));
      }
      if (!gone && again) timer = window.setTimeout(load, 5000);
    }
    void load();
    return () => { gone = true; window.clearTimeout(timer); };
  }, [targetRunId, initial?.status, refresh]);

  const item = trace?.run ?? initial;
  const plan = item ? planFor(item) : {};
  const graph = trace?.graph;
  const policy: WorkflowPolicy | undefined = trace?.policy ?? graph?.policy;

  const activeSelection = useMemo<CanvasSelection>(() => {
    if (selection) return selection;
    if (graph && item) {
      if (item.current_position != null) {
        const curId = `step:${item.current_position}`;
        if (graph.nodes.some((n) => n.id === curId)) return { type: "node", id: curId };
      }
      if (item.status === "done") {
        const signoffNode = graph.nodes.find((n) => n.id === "human:signoff");
        if (signoffNode) return { type: "node", id: signoffNode.id };
      }
      if (graph.nodes[0]) return { type: "node", id: graph.nodes[0].id };
    }
    return { type: "node", id: `step:${item?.current_position ?? 1}` };
  }, [selection, graph, item]);

  if (!item) {
    return (
      <div className="rounded-xl border border-dh-bsoft bg-dh-soft p-6 text-center text-sm text-dh-muted">
        {error ? `读取白盒工作流失败：${error}` : "正在加载白盒工作流…"}
      </div>
    );
  }

  function handleSelect(next: CanvasSelection) {
    setSelection(next);
    if (next.type === "node" && next.id === "human:signoff") {
      setTab("signoff");
    } else if (next.type === "node" && tab === "signoff" && next.id !== "human:signoff") {
      setTab("gates");
    }
  }

  function openPolicyEditor() {
    if (policy) {
      setDraftTrust(policy.trust_level);
      setDraftDiffGate(policy.diff_integrity_gate);
      setDraftSignoff(policy.require_product_signoff);
      setDraftGlobs(policy.auto_merge_globs.join("\n"));
    }
    setPolicyOpen((v) => !v);
  }

  async function savePolicy() {
    if (!item) return;
    setPolicySaving(true);
    try {
      await api.workflowPolicy.update(item.project_id, {
        trust_level: draftTrust,
        diff_integrity_gate: draftDiffGate,
        require_product_signoff: draftSignoff,
        auto_merge_globs: draftGlobs.split("\n").map((s) => s.trim()).filter(Boolean),
      });
      toast("项目白盒交付策略已更新");
      setPolicyOpen(false);
      setRefresh((v) => v + 1);
    } catch (e) {
      toast(`保存策略失败：${String(e)}`, "error");
    } finally {
      setPolicySaving(false);
    }
  }

  async function openTerminalSafely(taskId: number, stepStatus?: string) {
    if (stepStatus === "running" || stepStatus === "queued") {
      const ok = await confirmDialog(
        `子任务 #${taskId} 正在自动执行中。打开原始终端后请优先只读观察，避免误输入打断交接流程。确定进入终端？`,
      );
      if (!ok) return;
    }
    try {
      onOpenTask(await api.getTask(taskId));
    } catch (e) {
      toast(String(e), "error");
    }
  }

  async function submitSignoff(decision: "approved" | "rejected_impl" | "rejected_spec") {
    if (!item) return;
    if (decision !== "approved" && !signoffComment.trim()) {
      toast("退回时请先填写具体问题或口径说明", "error");
      return;
    }
    setSignoffBusy(true);
    try {
      await api.signoffRun(item.id, { decision, signer: signer.trim() || "产品负责人", comment: signoffComment.trim() });
      toast(decision === "approved" ? "已记录产品确认" : "已记录退回意见并更新白盒轨迹");
      setSignoffComment("");
      setRefresh((v) => v + 1);
    } catch (e) {
      toast(`提交签收失败：${String(e)}`, "error");
    } finally {
      setSignoffBusy(false);
    }
  }

  const selectedNode: WorkflowNode | undefined =
    activeSelection.type === "node" ? graph?.nodes.find((n) => n.id === activeSelection.id) : undefined;
  const selectedEdge =
    activeSelection.type === "edge" ? graph?.edges.find((e) => e.id === activeSelection.id) : undefined;

  const selectedPosition = selectedNode?.kind === "agent" ? selectedNode.position : null;
  const step = selectedPosition != null ? item.steps.find((s) => s.position === selectedPosition) : undefined;
  const stages = plan.steps?.length ? plan.steps : item.steps;
  const stage = selectedPosition != null ? stages.find((s) => s.position === selectedPosition) : undefined;

  const decisions = selectedPosition != null ? (trace?.decisions.filter((d) => d.position === selectedPosition) ?? []) : [];
  const checks = selectedPosition != null ? (trace?.checks.filter((c) => c.position === selectedPosition) ?? []) : (trace?.checks ?? []);
  const archives = selectedPosition != null ? (trace?.archives.filter((a) => a.position === selectedPosition) ?? []) : [];
  const edgeMessages = selectedEdge
    ? (trace?.messages.filter((m) => selectedEdge.message_ids.includes(m.id)) ?? [])
    : [];

  return (
    <div className="space-y-4 text-sm text-dh-tsoft">
      {/* 顶部白盒概览条 */}
      <header className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-dh-bsoft bg-dh-soft px-4 py-2.5">
        <div className="flex flex-wrap items-center gap-2.5">
          <span className="text-sm font-semibold text-dh-text">白盒工作流</span>
          {policy && (
            <button
              type="button"
              onClick={openPolicyEditor}
              title={policy.level_summary}
              className="rounded-md border border-dh-accent/40 bg-dh-accent/10 px-2.5 py-0.5 text-xs font-medium text-dh-accent hover:bg-dh-accent/20"
            >
              {policy.level_code} · {policy.level_name} ⚙
            </button>
          )}
          {graph && (
            <span className="rounded-md bg-dh-surface px-2.5 py-0.5 text-xs text-dh-tsoft">
              门禁 {graph.summary.passed_gates}/{graph.summary.total_gates} 通过
              {graph.summary.failed_gates > 0 && (
                <span className="ml-1.5 font-medium text-rose-400">· {graph.summary.failed_gates} 未通过</span>
              )}
            </span>
          )}
          {graph?.candidate_version && (
            <span
              title={`候选提交：${graph.candidate_version}`}
              className="rounded-md bg-dh-surface px-2.5 py-0.5 font-mono text-xs text-dh-muted"
            >
              SHA {graph.candidate_version.slice(0, 8)}
            </span>
          )}
        </div>
        <div className="flex items-center gap-3 text-xs text-dh-muted">
          <span>{synced ? "同步于 " + when(synced) : "正在读取"}</span>
          <button type="button" className="underline hover:text-dh-text" onClick={() => setRefresh((v) => v + 1)}>
            刷新
          </button>
        </div>
      </header>

      {/* 项目信任等级与白盒策略配置面板 */}
      {policyOpen && (
        <div className="space-y-3 rounded-xl border border-dh-accent/40 bg-dh-surface p-4 text-xs">
          <div className="flex items-center justify-between">
            <strong className="text-sm text-dh-text">项目信任授权与白盒护栏配置</strong>
            <button type="button" onClick={() => setPolicyOpen(false)} className="text-dh-muted underline">关闭</button>
          </div>
          <div className="grid gap-4 md:grid-cols-2">
            <label className="space-y-1.5">
              <span className="block font-medium text-dh-text">信任等级上限（L0–L4）</span>
              <select
                value={draftTrust}
                onChange={(e) => setDraftTrust(Number(e.target.value))}
                className="w-full rounded-lg border border-dh-bsoft bg-dh-soft px-3 py-2 text-xs text-dh-text"
              >
                {TRUST_LEVEL_OPTIONS.map((opt) => (
                  <option key={opt.level} value={opt.level}>{opt.label} — {opt.desc}</option>
                ))}
              </select>
            </label>
            <div className="space-y-2.5 pt-1">
              <label className="flex items-center gap-2">
                <input type="checkbox" checked={draftDiffGate} onChange={(e) => setDraftDiffGate(e.target.checked)} />
                <span className="text-dh-text">开启 Diff 完整性护栏（拦截路径越界 / 新增 skip / noqa / 空异常）</span>
              </label>
              <label className="flex items-center gap-2">
                <input type="checkbox" checked={draftSignoff} onChange={(e) => setDraftSignoff(e.target.checked)} />
                <span className="text-dh-text">需要产品验收卡签收节点</span>
              </label>
            </div>
          </div>
          {draftTrust === 3 && (
            <label className="block space-y-1.5">
              <span className="block font-medium text-dh-text">L3 自动合并允许路径白名单（每行一个 glob）</span>
              <textarea
                rows={3}
                value={draftGlobs}
                onChange={(e) => setDraftGlobs(e.target.value)}
                placeholder="docs/**&#10;frontend/src/styles/**"
                className="w-full rounded-lg border border-dh-bsoft bg-dh-soft p-2.5 font-mono text-xs text-dh-text"
              />
            </label>
          )}
          <div className="flex justify-end gap-2">
            <button type="button" onClick={() => setPolicyOpen(false)} className="rounded-md border border-dh-bsoft px-3 py-1.5 text-dh-tsoft">取消</button>
            <button
              type="button"
              disabled={policySaving}
              onClick={() => void savePolicy()}
              className="rounded-md bg-dh-accent px-3.5 py-1.5 font-medium text-dh-accfg disabled:opacity-50"
            >
              {policySaving ? "保存中…" : "保存策略"}
            </button>
          </div>
        </div>
      )}

      {error && <p role="alert" className="text-xs text-amber-500">读取执行记录失败 · {error}</p>}

      {/* 交互式三泳道工作流拓扑画布 */}
      {graph && (
        <WorkflowCanvas
          graph={graph}
          selection={activeSelection}
          onSelect={handleSelect}
        />
      )}

      {/* 白盒检查器（选中连线） */}
      {selectedEdge && (
        <section className="space-y-4 rounded-xl border border-dh-bsoft bg-dh-soft/70 p-4" aria-label="连线交接详情">
          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-dh-bsoft pb-3">
            <div>
              <h3 className="text-base font-semibold text-dh-text">{selectedEdge.label}</h3>
              <p className="mt-0.5 text-xs text-dh-muted">
                {graph?.nodes.find((n) => n.id === selectedEdge.source)?.title ?? selectedEdge.source}
                {" → "}
                {graph?.nodes.find((n) => n.id === selectedEdge.target)?.title ?? selectedEdge.target}
              </p>
            </div>
            <span className="rounded-md bg-dh-surface px-2.5 py-1 text-xs text-dh-muted">
              {selectedEdge.kind === "rework" ? "返工回环" : selectedEdge.kind === "ask" ? "角色追问" : "主线交接"}
            </span>
          </div>
          {edgeMessages.length === 0 ? (
            <p className="text-xs text-dh-muted">该连线为确定性阶段流转，没有附加的角色间自由文本消息。</p>
          ) : (
            <div className="space-y-3">
              {edgeMessages.map((m) => (
                <div key={m.id} className="rounded-xl border border-dh-bsoft bg-dh-surface p-3.5">
                  <div className="mb-2 flex flex-wrap items-center justify-between gap-2 text-xs text-dh-muted">
                    <span>
                      <strong className="text-dh-text">{statusText(m.kind)}</strong> · {when(m.created_at)}
                    </span>
                    <span>
                      {m.acknowledged_at ? `已确认 (${when(m.acknowledged_at)})` : m.delivered_at ? `已投递 (${when(m.delivered_at)})` : "待投递"}
                    </span>
                  </div>
                  <pre className="max-h-72 overflow-auto whitespace-pre-wrap break-words font-mono text-xs leading-relaxed text-dh-tsoft">{m.body}</pre>
                </div>
              ))}
            </div>
          )}
        </section>
      )}

      {/* 白盒检查器（选中节点）— 宽松留白、结构化卡片呈现 */}
      {selectedNode && (
        <section className="space-y-5 rounded-xl border border-dh-bsoft bg-dh-soft/60 p-5" aria-label="节点白盒检查器">
          {/* 节点头部 */}
          <div className="flex flex-wrap items-start justify-between gap-3 border-b border-dh-bsoft pb-4">
            <div className="space-y-1">
              <div className="flex flex-wrap items-center gap-2.5">
                <h3 className="text-base font-semibold text-dh-text">{selectedNode.title}</h3>
                <span className="rounded-full bg-dh-surface px-2.5 py-0.5 text-xs font-medium text-dh-tsoft">
                  {statusText(selectedNode.status)}
                </span>
                {selectedNode.attempt > 0 && (
                  <span className="text-xs text-dh-muted">第 {selectedNode.attempt} 轮</span>
                )}
              </div>
              {step && (
                <p className="text-xs text-dh-muted">
                  开始：{when(step.started_at)}　·　结束：{when(step.ended_at)}
                </p>
              )}
            </div>
            {step?.task_id && (
              <button
                type="button"
                onClick={() => void openTerminalSafely(step.task_id!, step.status)}
                className="rounded-lg border border-dh-accent/50 bg-dh-accent/10 px-3 py-1.5 text-xs font-medium text-dh-accent hover:bg-dh-accent/20"
              >
                进入子任务 #{step.task_id} 终端 ↗
              </button>
            )}
          </div>

          {/* 清晰的分段 Tab */}
          <div className="flex flex-wrap gap-2">
            {([
              ["gates", `门禁与判定 (${selectedNode.gates.length})`],
              ["io", "产物与输入输出"],
              ["evidence", `实测证据 (${checks.length})`],
              ["timeline", `沟通与生命周期 (${(trace?.messages.length ?? 0) + (trace?.decisions.length ?? 0) + (trace?.signoffs?.length ?? 0)})`],
              ["signoff", `产品验收卡 (${trace?.signoffs?.length ?? 0})`],
            ] as const).map(([key, label]) => (
              <button
                key={key}
                type="button"
                onClick={() => setTab(key)}
                className={`rounded-lg px-3.5 py-1.5 text-xs font-medium transition ${
                  tab === key
                    ? "bg-dh-accent text-dh-accfg shadow-sm"
                    : "border border-dh-bsoft bg-dh-surface text-dh-tsoft hover:text-dh-text"
                }`}
              >
                {label}
              </button>
            ))}
          </div>

          {/* Tab 1: 门禁与判定 */}
          {tab === "gates" && (
            <div className="space-y-5">
              <div className="grid gap-3 md:grid-cols-2">
                {selectedNode.gates.map((g) => {
                  const st = gateStyle(g.status);
                  return (
                    <div key={g.id} className={`rounded-xl border p-3.5 ${st.card}`}>
                      <div className="flex items-center justify-between gap-2">
                        <span className="text-sm font-semibold text-dh-text">{g.label}</span>
                        <span className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium ${st.badge}`}>
                          <span>{st.icon}</span>
                          <span>{st.text}</span>
                        </span>
                      </div>
                      <p className="mt-2 text-[13px] leading-relaxed text-dh-tsoft">{g.detail}</p>
                    </div>
                  );
                })}
              </div>

              {selectedNode.id === "sys:merge" && graph && (
                <div className="rounded-xl border border-dh-bsoft bg-dh-surface p-4">
                  <div className="text-sm font-semibold text-dh-text">合并策略判决（纯确定性）</div>
                  <p className="mt-1.5 text-[13px] leading-relaxed text-dh-tsoft">{graph.merge_policy.reason}</p>
                </div>
              )}

              {step?.summary && (
                <div className="rounded-xl border border-dh-bsoft bg-dh-surface p-4">
                  <div className="text-xs font-semibold uppercase tracking-wide text-dh-muted">本轮阶段摘要</div>
                  <p className="mt-1.5 whitespace-pre-wrap text-[13px] leading-relaxed text-dh-text">{step.summary}</p>
                </div>
              )}

              {selectedNode.kind === "agent" && (
                <div className="space-y-3">
                  <h4 className="text-xs font-semibold uppercase tracking-wide text-dh-muted">交接判定审计</h4>
                  {decisions.length === 0 ? (
                    <p className="text-xs text-dh-muted">
                      {step?.ended_at ? "该阶段按默认流转完成，无额外拦截记录。" : "暂无交接判定记录。"}
                    </p>
                  ) : (
                    decisions.map((d) => (
                      <div key={d.id} className="rounded-xl border border-dh-bsoft bg-dh-surface p-4 space-y-1.5">
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <span className="text-sm font-semibold text-dh-text">
                            {statusText(d.result)} · 第 {d.attempt} 轮
                          </span>
                          <span className="text-xs text-dh-muted">{when(d.created_at)}</span>
                        </div>
                        <p className="text-[13px] text-dh-tsoft">
                          角色申报「{statusText(d.requested)}」 → 编排判定「{statusText(d.result)}」
                        </p>
                        <p className="text-xs text-dh-muted">判定依据：{d.basis}</p>
                        {d.reason && <p className="mt-1 whitespace-pre-wrap text-[13px] leading-relaxed text-amber-400">{d.reason}</p>}
                        {!!d.receipts.length && (
                          <p className="text-xs text-dh-muted">关联回执：{d.receipts.map((n) => `#${n}`).join("、")}</p>
                        )}
                      </div>
                    ))
                  )}
                </div>
              )}
            </div>
          )}

          {/* Tab 2: 产物与输入输出（结构化阅读 + Markdown 排版） */}
          {tab === "io" && (
            <div className="space-y-4">
              {selectedNode.kind === "agent" ? (
                <>
                  <ArtifactViewer title="交付的输出（Output Artifact）" value={step?.output_artifact} defaultOpen />
                  <ArtifactViewer title="收到的输入（Input Artifact）" value={step?.input_artifact} />
                  <details className="rounded-xl border border-dh-bsoft bg-dh-surface p-4">
                    <summary className="cursor-pointer text-sm font-semibold text-dh-text">本次角色执行约定（Role Prompt 快照）</summary>
                    <pre className="mt-3 max-h-80 overflow-auto whitespace-pre-wrap break-words font-mono text-xs leading-relaxed text-dh-tsoft">
                      {stage?.role_prompt || "尚无记录"}
                    </pre>
                  </details>
                  {!!archives.length && (
                    <div className="space-y-2.5">
                      <h4 className="text-xs font-semibold uppercase tracking-wide text-dh-muted">返工前归档版本（{archives.length} 轮）</h4>
                      {archives.map((a) => (
                        <details key={a.id} className="rounded-xl border border-dh-bsoft bg-dh-surface p-3.5">
                          <summary className="cursor-pointer text-xs font-medium text-dh-text">
                            第 {a.attempt} 轮 · 归档于 {when(a.created_at)}
                          </summary>
                          <div className="mt-3 space-y-3">
                            <ArtifactViewer title="当轮输出" value={a.output_artifact} />
                            <ArtifactViewer title="当轮输入" value={a.input_artifact} />
                          </div>
                        </details>
                      ))}
                    </div>
                  )}
                </>
              ) : (
                <ArtifactViewer title="原始需求描述" value={item.original_prompt} defaultOpen />
              )}
            </div>
          )}

          {/* Tab 3: 实测证据与对比图 */}
          {tab === "evidence" && (
            <div className="space-y-3">
              {checks.length === 0 ? (
                <p className="text-xs text-dh-muted">当前节点暂无远程实测回执。</p>
              ) : (
                checks.map((c) => (
                  <details key={c.id} open className="rounded-xl border border-dh-bsoft bg-dh-surface p-4">
                    <summary className="cursor-pointer text-sm font-semibold text-dh-text">
                      回执 #{c.id} · 第 {c.attempt} 轮 · {c.items.join("、")} ·{" "}
                      <span className={c.state === "passed" ? "text-emerald-400" : c.state === "failed" ? "text-rose-400" : "text-amber-400"}>
                        {statusText(c.state)}
                      </span>
                    </summary>
                    <p className="mt-2 break-all text-xs text-dh-muted">
                      测试机：{c.host}　·　候选提交：<span className="font-mono">{c.version}</span>
                    </p>
                    <pre className="mt-2 overflow-x-auto rounded-lg bg-dh-soft p-3 font-mono text-xs">{c.command}</pre>
                    <pre className="mt-2 max-h-56 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-dh-soft p-3 font-mono text-xs leading-relaxed">
                      {c.detail || "尚无执行结果"}
                    </pre>
                    {c.outcomes && <ReceiptEvidencePreview runId={item.id} receiptId={c.id} />}
                  </details>
                ))
              )}
            </div>
          )}

          {/* Tab 3.5: 编排主任务完整生命周期（子任务间交接、返工、问答、门禁判定与签收时间线） */}
          {tab === "timeline" && (() => {
            const stepName = (pos: number | null | undefined) => {
              if (pos == null) return "系统编排器";
              return stages.find((s) => s.position === pos)?.name || `步骤 ${pos}`;
            };
            const timelineItems: { key: string; ts: number; kind: string; tone: string; title: string; subtitle: string; body: string }[] = [
              ...(trace?.messages ?? []).map((m) => ({
                key: `m-${m.id}`,
                ts: m.created_at,
                kind: statusText(m.kind),
                tone: m.kind === "rework"
                  ? "border-l-amber-400"
                  : m.kind === "handoff"
                    ? "border-l-emerald-500"
                    : "border-l-violet-400",
                title: `${stepName(m.from_position)} → ${stepName(m.to_position)}`,
                subtitle: m.acknowledged_at ? "对方已确认" : m.delivered_at ? "已投递" : "待投递",
                body: m.body,
              })),
              ...(trace?.decisions ?? []).map((d) => ({
                key: `d-${d.id}`,
                ts: d.created_at,
                kind: `门禁 · ${statusText(d.result)}`,
                tone: d.result === "rejected" || d.result === "rework" ? "border-l-rose-500" : "border-l-sky-400",
                title: `${stepName(d.position)}（第 ${d.attempt} 轮）`,
                subtitle: `申报「${statusText(d.requested)}」 · ${d.basis}`,
                body: d.reason || "判定通过",
              })),
              ...(trace?.signoffs ?? []).map((s) => ({
                key: `s-${s.id}`,
                ts: s.created_at,
                kind: statusText(s.decision),
                tone: s.decision === "approved" ? "border-l-emerald-500" : "border-l-amber-400",
                title: `产品验收 · ${s.signer}`,
                subtitle: s.commit_sha ? `版本 ${s.commit_sha.slice(0, 10)}` : "人工签收",
                body: s.comment || "确认符合预期",
              })),
            ].sort((a, b) => a.ts - b.ts);

            return (
              <div className="space-y-3">
                {timelineItems.length === 0 ? (
                  <p className="text-xs text-dh-muted">尚无子任务间交接、返工或审批记录。</p>
                ) : (
                  timelineItems.map((ev) => (
                    <div key={ev.key} className={`rounded-xl border border-dh-bsoft border-l-4 ${ev.tone} bg-dh-surface p-3.5 space-y-1.5`}>
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="rounded bg-dh-s2 px-2 py-0.5 text-[11px] font-medium text-dh-text">{ev.kind}</span>
                          <span className="text-sm font-semibold text-dh-text">{ev.title}</span>
                        </div>
                        <span className="text-xs text-dh-muted">{when(ev.ts)}</span>
                      </div>
                      <div className="text-xs text-dh-muted">{ev.subtitle}</div>
                      {ev.body && (
                        <pre className="mt-1 max-h-56 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-dh-soft/70 p-2.5 font-mono text-xs leading-relaxed text-dh-tsoft">
                          {ev.body}
                        </pre>
                      )}
                    </div>
                  ))
                )}
              </div>
            );
          })()}

          {/* Tab 4: 产品验收卡 */}
          {tab === "signoff" && (
            <div className="space-y-4">
              {item.acceptance ? (
                <div className="rounded-xl border border-dh-bsoft bg-dh-surface p-4">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <h4 className="text-sm font-semibold text-dh-text">验收表 · {item.acceptance.message}</h4>
                    {item.acceptance.version && (
                      <span className="font-mono text-xs text-dh-muted">版本 {item.acceptance.version.slice(0, 12)}</span>
                    )}
                  </div>
                  {item.acceptance.rows.length > 0 && (
                    <div className="mt-3 overflow-x-auto">
                      <table className="w-full text-left text-xs">
                        <thead>
                          <tr className="border-b border-dh-bsoft text-dh-muted">
                            {["必验项", "输入与预期", "结论", "实际结果", "证据"].map((h) => (
                              <th key={h} className="p-2.5">{h}</th>
                            ))}
                          </tr>
                        </thead>
                        <tbody>
                          {item.acceptance.rows.map((row, idx) => (
                            <tr key={idx} className="border-t border-dh-bsoft align-top">
                              <td className="p-2.5 font-medium text-dh-text">{row.id}</td>
                              <td className="p-2.5 leading-relaxed">{[row.input, row.expected].filter(Boolean).join(" → ") || "—"}</td>
                              <td className="whitespace-nowrap p-2.5">
                                <span className={row.status === "通过" ? "text-emerald-400" : row.status === "失败" ? "text-rose-400" : "text-amber-400"}>
                                  {row.status}
                                </span>
                              </td>
                              <td className="p-2.5 leading-relaxed">{row.actual || "—"}</td>
                              <td className="break-all p-2.5">
                                {row.evidence || "—"}
                                {row.receipt_id && (
                                  <button
                                    type="button"
                                    className="mt-1 block text-sky-400 underline"
                                    onClick={() => void downloadEvidence(item.id, row.receipt_id!)}
                                  >
                                    下载检查报告与证据
                                  </button>
                                )}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </div>
              ) : (
                <p className="text-xs text-dh-muted">当前编排未启用结构化验收表，可直接基于阶段输出与证据进行人工签收。</p>
              )}

              {/* 签收操作表单 */}
              <div className="space-y-3 rounded-xl border border-dh-bsoft bg-dh-surface p-4">
                <div className="text-sm font-semibold text-dh-text">产品 / 负责人签收操作</div>
                <div className="grid gap-3 md:grid-cols-3">
                  <input
                    type="text"
                    value={signer}
                    onChange={(e) => setSigner(e.target.value)}
                    placeholder="签收人（默认：产品负责人）"
                    className="rounded-lg border border-dh-bsoft bg-dh-soft px-3 py-2 text-xs text-dh-text"
                  />
                  <input
                    type="text"
                    value={signoffComment}
                    onChange={(e) => setSignoffComment(e.target.value)}
                    placeholder="补充意见（退回时必填具体不符点或口径变化）"
                    className="rounded-lg border border-dh-bsoft bg-dh-soft px-3 py-2 text-xs text-dh-text md:col-span-2"
                  />
                </div>
                <div className="flex flex-wrap gap-2.5 pt-1">
                  <button
                    type="button"
                    disabled={signoffBusy}
                    onClick={() => void submitSignoff("approved")}
                    className="rounded-lg bg-emerald-600 px-3.5 py-2 text-xs font-medium text-white hover:bg-emerald-500 disabled:opacity-50"
                  >
                    确认符合预期
                  </button>
                  <button
                    type="button"
                    disabled={signoffBusy}
                    onClick={() => void submitSignoff("rejected_impl")}
                    className="rounded-lg border border-amber-500/50 bg-amber-500/15 px-3.5 py-2 text-xs font-medium text-amber-300 hover:bg-amber-500/25 disabled:opacity-50"
                  >
                    实现不符 · 退回开发
                  </button>
                  <button
                    type="button"
                    disabled={signoffBusy}
                    onClick={() => void submitSignoff("rejected_spec")}
                    className="rounded-lg border border-rose-500/50 bg-rose-500/15 px-3.5 py-2 text-xs font-medium text-rose-300 hover:bg-rose-500/25 disabled:opacity-50"
                  >
                    口径变更 · 退回需求
                  </button>
                </div>
              </div>

              {/* 历史签收记录 */}
              {!!trace?.signoffs?.length && (
                <div className="space-y-2">
                  <h4 className="text-xs font-semibold uppercase tracking-wide text-dh-muted">签收历史</h4>
                  {trace.signoffs.map((s) => (
                    <div key={s.id} className="rounded-xl border border-dh-bsoft bg-dh-surface p-3 text-xs">
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <span className="font-semibold text-dh-text">
                          {statusText(s.decision)} · {s.signer}
                        </span>
                        <span className="text-dh-muted">
                          {s.commit_sha ? `SHA ${s.commit_sha.slice(0, 8)} · ` : ""}{when(s.created_at)}
                        </span>
                      </div>
                      {s.comment && <p className="mt-1.5 leading-relaxed text-dh-tsoft">{s.comment}</p>}
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </section>
      )}

      {/* 底部折叠：原始需求与启用的规则 */}
      <details className="rounded-xl border border-dh-bsoft bg-dh-surface/60 p-3.5 text-xs">
        <summary className="cursor-pointer font-medium text-dh-text">原始需求与运行时规则快照</summary>
        <p className="mt-2 text-dh-muted">
          启用规则：{[plan.acceptance_gate && "验收清单", plan.remote_checks && "远程实测", plan.specialist_checks && "专项报告核验"].filter(Boolean).join("、") || "阶段回报"}
        </p>
        <pre className="mt-2 max-h-60 overflow-auto whitespace-pre-wrap break-words font-mono text-xs leading-relaxed text-dh-tsoft">{item.original_prompt}</pre>
      </details>
    </div>
  );
}
