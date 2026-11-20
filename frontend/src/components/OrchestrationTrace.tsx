import { useEffect, useState } from "react";
import { api } from "../api";
import { toast } from "../overlay";
import type { OrchestrationRun, RunTrace, Task } from "../types";

const labels: Record<string, string> = {
  draft: "待启动", queued: "已排队", running: "执行中", done: "已完成", interrupted: "已中断", offline: "已掉线",
  failed: "失败", cancelled: "已取消", waiting_input: "等待输入", needs_input: "需要补充", rework: "退回返工",
  rejected: "交接被拦截", passed: "通过", error: "未完成验证", handoff: "交接", ask: "提问", answer: "答复", system: "系统记录",
};
export const statusText = (state: string) => labels[state] || state;
const live = (state: string) => ["running", "queued", "waiting_input"].includes(state);
const when = (n: number | null) => n ? new Date(n * 1000).toLocaleString("zh-CN", {hour12: false}) : "—";

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


function RecordText({ title, value }: { title: string; value?: string | null }) {
  return <details className="rounded-lg border border-dh-bsoft p-2">
    <summary className="cursor-pointer font-medium">{title}</summary>
    <pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap break-words">{value || "尚无记录"}</pre>
  </details>;
}

type Plan = { steps?: { position: number; name: string; role_prompt: string }[]; acceptance_gate?: boolean; remote_checks?: boolean; specialist_checks?: boolean };
function planFor(item: OrchestrationRun): Plan {
  try { return JSON.parse(item.definition_snapshot); } catch { return {}; }
}

export function OrchestrationTrace({ initial, onOpenTask }: { initial: OrchestrationRun; onOpenTask: (t: Task) => void }) {
  const [trace, setTrace] = useState<RunTrace | null>(null);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [selected, setSelected] = useState<number | null>(null);
  const [synced, setSynced] = useState<number | null>(null);
  useEffect(() => {
    let gone = false;
    let timer: number;
    async function load() {
      let again = true;
      try {
        if (!document.hidden) {
          const data = await api.runTrace(initial.id);
          if (gone) return;
          setTrace(data); setError(""); setSynced(Date.now() / 1000);
          again = live(data.run.status);
        }
      } catch (e) { if (!gone) setError(String(e)); }
      if (!gone && again) timer = window.setTimeout(load, 5000);
    }
    void load();
    return () => { gone = true; window.clearTimeout(timer); };
  }, [initial.id, initial.status, refresh]);
  const item = trace?.run ?? initial;
  const plan = planFor(item);
  const stages = plan.steps?.length ? plan.steps : item.steps;
  const position = selected ?? (item.status === "done" ? trace?.checks[0]?.position : null) ?? item.current_position ?? stages[0]?.position;
  const step = item.steps.find(s => s.position === position);
  const stage = stages.find(s => s.position === position);
  const decisions = trace?.decisions.filter(d => d.position === position) ?? [];
  const latest = decisions[decisions.length - 1];
  const active = item.steps.find(s => s.position === item.current_position);
  const checks = trace?.checks.filter(c => c.position === position) ?? [];
  const history = trace?.archives.filter(a => a.position === position) ?? [];
  const messages = trace?.messages.filter(m => m.from_position === position || m.to_position === position) ?? [];
  async function terminal(id: number) {
    try { onOpenTask(await api.getTask(id)); } catch (e) { toast(String(e), "error"); }
  }
  return <div className="space-y-3 text-xs text-dh-tsoft">
    <header className="flex flex-wrap items-center justify-between gap-2">
      <h3 className="text-sm font-semibold text-dh-text">执行轨迹</h3>
      <div className="flex items-center gap-3 text-dh-muted"><span>{synced ? "更新于 " + when(synced) : "正在读取"}</span>
        <button type="button" className="underline" onClick={() => setRefresh(v => v+1)}>刷新</button></div>
    </header>
    {error && <p role="alert" className="text-amber-500">读取执行记录失败 · {error}</p>}
    <p>{live(item.status) ? "当前负责" : "运行结果"}　<strong>{live(item.status) ? active?.name ?? statusText(item.status) : statusText(item.status)}</strong>
      {item.status === "waiting_input" && active?.summary && <span> · {active.summary}</span>}</p>
    <nav aria-label="选择执行阶段" className="flex flex-wrap gap-2">{stages.map(s => {
      const actual = item.steps.find(v => v.position === s.position);
      return <button key={s.position} type="button" aria-pressed={s.position === position} onClick={() => setSelected(s.position)}
        className={"min-w-32 flex-1 rounded-lg border px-3 py-2 text-left " + (s.position === position ? "border-dh-accent bg-dh-soft" : "border-dh-bsoft")}>
        <span className="block font-semibold text-dh-text">{s.name}</span>
        <span>{actual ? statusText(actual.status) + " · 第 " + actual.attempt + " 轮" : "尚未创建任务"}</span>
      </button>;
    })}</nav>
    <section className="space-y-2 rounded-xl bg-dh-soft p-3" aria-label="阶段详情">
      <div className="flex flex-wrap items-center justify-between gap-2"><strong>{stage?.name}</strong>
        {step?.task_id && <button type="button" className="underline" onClick={() => void terminal(step.task_id!)}>查看任务 #{step.task_id} 执行终端</button>}</div>
      {step && <p>开始　{when(step.started_at)}　结束　{when(step.ended_at)}</p>}
      {step?.summary && <p className="whitespace-pre-wrap">{step.summary}</p>}
      {latest?.result === "rejected" && latest.attempt === step?.attempt && live(item.status) && <p className="text-amber-500">最近交接被拦截 · {latest.reason}</p>}
      <div className="grid gap-2 md:grid-cols-2"><RecordText title="收到的输入" value={step?.input_artifact} /><RecordText title="交付的输出" value={step?.output_artifact}/></div>
      <RecordText title="本次角色执行约定" value={stage?.role_prompt}/>
      <h4 className="pt-2 font-semibold">交接判定</h4>
      {decisions.length === 0 && <p className="text-dh-muted">{step?.ended_at ? "该阶段没有单独保存判定事件，可核对阶段回报、交接消息与检查回执。" : "暂无已记录的交接判定。"}</p>}
      {decisions.map(d => <div key={d.id} className="rounded bg-dh-surface p-2"><b>{statusText(d.result)}</b> · 第 {d.attempt} 轮 · {when(d.created_at)}
        <p>角色申报「{statusText(d.requested)}」 · 编排判定「{statusText(d.result)}」</p>
        <p>判定依据　{d.basis}</p>
        {d.reason && <p className="whitespace-pre-wrap">{d.reason}</p>}
        {!!d.receipts.length && <p>关联回执 {d.receipts.map(n => "#" + n).join("、")}</p>}</div>)}
      {!!checks.length && <div><h4 className="mb-1 font-semibold">历次远程检查</h4>{checks.map(c => <details key={c.id} className="mb-1 rounded bg-dh-surface p-2">
        <summary className="cursor-pointer">回执 #{c.id} · 第 {c.attempt} 轮 · {c.items.join("、")} · {statusText(c.state)}</summary>
        <p className="mt-1 break-all">测试机　{c.host}<br/>候选提交　{c.version}</p>
        <pre className="whitespace-pre-wrap break-words">{c.command}</pre><pre className="whitespace-pre-wrap break-words">{c.detail || "尚无执行结果"}</pre>
        {c.outcomes && <button type="button" className="underline" onClick={() => void downloadEvidence(item.id,c.id)}>下载本次证据</button>}</details>)}</div>}
      {!!history.length && <div><h4 className="font-semibold">返工前的版本</h4>{history.map(a => <details key={a.id} className="p-2"><summary className="cursor-pointer">第 {a.attempt} 轮 · 归档于 {when(a.created_at)}</summary>
        <RecordText title="当轮输入" value={a.input_artifact}/><RecordText title="当轮输出" value={a.output_artifact}/></details>)}</div>}
      {!!messages.length && <details><summary className="cursor-pointer">交接与问答 · {messages.length} 条</summary>{messages.map(m => <details key={m.id} className="mt-1 rounded bg-dh-surface p-2">
        <summary className="cursor-pointer">{when(m.created_at)} · {statusText(m.kind)} · {m.from_position ?? "系统"} → {m.to_position} · {m.acknowledged_at ? "已确认" : m.delivered_at ? "已投递" : "待投递"}</summary>
        <pre className="whitespace-pre-wrap break-words">{m.body}</pre></details>)}</details>}
    </section>
    <details className="p-2"><summary className="cursor-pointer">原始需求与本次启用的规则</summary>
      <p>按运行时快照　{[plan.acceptance_gate && "验收清单", plan.remote_checks && "远程实测", plan.specialist_checks && "专项报告核验"].filter(Boolean).join("、") || "阶段回报"}</p>
      <pre className="whitespace-pre-wrap break-words">{item.original_prompt}</pre></details>
              {item.acceptance && (
                <section aria-label="验收表" className="rounded-lg border border-dh-bsoft p-3 text-xs">
                  <h3 className="font-semibold text-dh-text">验收表 · {item.acceptance.message}</h3>
                  {item.acceptance.version && <p className="mt-1 break-all text-dh-muted">候选版本　{item.acceptance.version}</p>}
                  {item.acceptance.rows.length > 0 && <div className="mt-2 overflow-x-auto"><table className="w-full text-left">
                    <thead><tr>{["必验项", "输入与预期", "结论", "实际结果", "证据"].map(label => <th key={label} className="p-2">{label}</th>)}</tr></thead>
                    <tbody>{item.acceptance.rows.map((row, index) => <tr key={index} className="border-t border-dh-bsoft align-top">
                      <td className="p-2">{row.id}</td><td className="p-2">{[row.input, row.expected].filter(Boolean).join(" → ") || "—"}</td>
                      <td className="whitespace-nowrap p-2">{row.status}</td><td className="p-2">{row.actual || "—"}</td><td className="break-all p-2">{row.evidence || "—"}
                        {row.receipt_id && <button type="button" className="block underline" onClick={() => void downloadEvidence(item.id, row.receipt_id!)}>下载检查报告与证据</button>}
                      </td>
                    </tr>)}</tbody></table></div>}
                  {item.acceptance.previous_review && <details className="mt-2"><summary className="cursor-pointer">查看上一轮质量记录</summary>
                    <pre className="mt-2 whitespace-pre-wrap break-words">{item.acceptance.previous_review}</pre></details>}
                </section>
              )}

  </div>;
}
