import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { confirmDialog, toast } from "../overlay";
import type { Project } from "../types";
import { useSingleFlight } from "../useSingleFlight";
import { AutopilotTrace } from "./AutopilotTrace";
import { Modal } from "./Modal";

const STATUS_LABEL: Record<string, { text: string; cls: string }> = {
  running: { text: "运行中", cls: "text-emerald-600" },
  done: { text: "完成", cls: "text-sky-600" },
  stopped: { text: "已停止", cls: "text-slate-500" },
  failed: { text: "失败", cls: "text-rose-600" },
};

export function AutopilotDialog({ project, onClose }: { project: Project; onClose: () => void }) {
  const [maxIter, setMaxIter] = useState(3);
  const [fixEngine, setFixEngine] = useState("cc");
  const [reviewEngine, setReviewEngine] = useState("codex");
  const [budgetK, setBudgetK] = useState(200); // 千 token；0=不限
  const [scope, setScope] = useState("recent");
  const [scopeArg, setScopeArg] = useState("3");
  const [run, setRun] = useState<any>(null);
  const [log, setLog] = useState("");
  const [view, setView] = useState<"trace" | "log">("trace");
  const { busy, run: runStart } = useSingleFlight();
  const logRef = useRef<HTMLPreElement>(null);

  const refresh = useCallback(async () => {
    const runs = await api.autopilot.list(project.id);
    const latest = runs[0] ?? null;
    setRun(latest);
    if (latest) setLog((await api.autopilot.log(latest.id)).log);
  }, [project.id]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // 运行中轮询日志
  useEffect(() => {
    if (run?.status !== "running") return;
    const t = setInterval(refresh, 2000);
    return () => clearInterval(t);
  }, [run?.status, refresh]);

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [log]);

  async function start(force = false) {
    await runStart(async () => {
      const body = {
        project_id: project.id,
        max_iterations: maxIter,
        fix_engine: fixEngine,
        review_engine: reviewEngine,
        token_budget: Math.max(0, budgetK) * 1000,
        scope,
        scope_arg: scope === "full" ? null : scopeArg,
        force,
      };
      try {
        const r = await api.autopilot.start(body);
        setRun(r);
        setLog("");
      } catch (e: any) {
        if (!force && /周用量|阈值|强制/.test(e.message || "")) {
          if (await confirmDialog(`${e.message}\n\n仍要强制启动吗？`, { danger: true })) {
            try {
              const r = await api.autopilot.start({ ...body, force: true });
              setRun(r);
              setLog("");
            } catch (e2: any) {
              toast(`启动失败：${e2.message}`, "error");
            }
          }
        } else {
          toast(`启动失败：${e.message}`, "error");
        }
      }
    });
  }

  const isRunning = run?.status === "running";
  const st = run ? STATUS_LABEL[run.status] : null;

  return (
    <Modal title={`自动修复（自愈）· ${project.name}`} onClose={onClose} wide>
      <div className="space-y-3 text-sm">
        <p className="rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-700">
          ⚠️ 会无人值守地改代码：在专用分支 <code>bosun/autopilot-*</code> 上循环「审→修→验→交叉复审→提交」。
          结束后请 review 分支 diff 再合并；不满意 <code>git reset</code> 回滚。会消耗较多 token。
        </p>

        {!isRunning && (
          <div className="flex flex-wrap items-end gap-4 rounded-lg border border-slate-200 p-3">
            <label className="flex flex-col gap-1">
              <span className="text-xs text-slate-500">审查范围</span>
              <select
                className="rounded-lg border border-slate-200 bg-white px-2 py-1"
                value={scope}
                onChange={(e) => setScope(e.target.value)}
              >
                <option value="recent">最近改动</option>
                <option value="full">全仓库</option>
                <option value="commit">指定 commit</option>
              </select>
            </label>
            {scope !== "full" && (
              <label className="flex flex-col gap-1">
                <span className="text-xs text-slate-500">
                  {scope === "recent" ? "最近N个提交" : "commit/范围"}
                </span>
                <input
                  className="w-24 rounded-lg border border-slate-200 bg-white px-2 py-1"
                  value={scopeArg}
                  onChange={(e) => setScopeArg(e.target.value)}
                />
              </label>
            )}
            <label className="flex flex-col gap-1">
              <span className="text-xs text-slate-500">最大迭代</span>
              <input
                type="number"
                min={1}
                max={10}
                className="w-20 rounded-lg border border-slate-200 bg-white px-2 py-1"
                value={maxIter}
                onChange={(e) => setMaxIter(Number(e.target.value))}
              />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-xs text-slate-500">修复引擎</span>
              <select
                className="rounded-lg border border-slate-200 bg-white px-2 py-1"
                value={fixEngine}
                onChange={(e) => setFixEngine(e.target.value)}
              >
                <option value="cc">Claude Code</option>
                <option value="codex">Codex</option>
                <option value="omp">Oh My Pi</option>
              </select>
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-xs text-slate-500">交叉复审引擎</span>
              <select
                className="rounded-lg border border-slate-200 bg-white px-2 py-1"
                value={reviewEngine}
                onChange={(e) => setReviewEngine(e.target.value)}
              >
                <option value="codex">Codex</option>
                <option value="cc">Claude Code</option>
                <option value="omp">Oh My Pi</option>
              </select>
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-xs text-slate-500">token 预算(千, 0=不限)</span>
              <input
                type="number"
                min={0}
                step={50}
                className="w-28 rounded-lg border border-slate-200 bg-white px-2 py-1"
                value={budgetK}
                onChange={(e) => setBudgetK(Number(e.target.value))}
              />
            </label>
            <button
              className="rounded-lg bg-emerald-500 px-4 py-1.5 font-medium text-white hover:bg-emerald-600 disabled:opacity-50"
              disabled={busy}
              onClick={() => start()}
            >
              ▶ 启动自愈
            </button>
          </div>
        )}

        {run && (
          <div className="rounded-lg border border-slate-200">
            <div className="flex items-center gap-3 border-b border-slate-200 px-3 py-2 text-xs">
              <span className={`font-medium ${st?.cls}`}>● {st?.text}</span>
              <span className="text-slate-500">
                第 {run.iteration}/{run.max_iterations} 轮
              </span>
              {run.branch && <span className="font-mono text-slate-400">{run.branch}</span>}
              <span className="text-slate-400">
                {run.fix_engine} 修 → {run.review_engine} 审
              </span>
              <span className="text-slate-500">
                🔥 {(run.tokens_used / 1000).toFixed(1)}k
                {run.token_budget > 0 ? ` / ${(run.token_budget / 1000).toFixed(0)}k` : " (不限)"} tok
              </span>
              {run.summary && <span className="text-slate-500">· {run.summary}</span>}
              <div className="ml-auto flex items-center gap-1">
                <div className="flex rounded-lg border border-slate-200 p-0.5">
                  {(["trace", "log"] as const).map((v) => (
                    <button
                      key={v}
                      onClick={() => setView(v)}
                      className={`rounded px-2 py-0.5 text-[11px] ${
                        view === v ? "bg-slate-800 text-white" : "text-slate-500 hover:bg-slate-100"
                      }`}
                    >
                      {v === "trace" ? "流水线" : "日志"}
                    </button>
                  ))}
                </div>
                {isRunning && (
                  <button
                    className="rounded-lg border border-slate-200 px-2 py-0.5 text-slate-600 hover:bg-rose-50 hover:text-rose-600 disabled:opacity-50"
                    disabled={busy}
                    onClick={() =>
                      runStart(async () => {
                        await api.autopilot.stop(run.id);
                        await refresh();
                      })
                    }
                  >
                    停止
                  </button>
                )}
              </div>
            </div>
            {view === "trace" ? (
              <div className="max-h-72 overflow-auto p-3">
                <AutopilotTrace runId={run.id} live={isRunning} />
              </div>
            ) : (
              <pre
                ref={logRef}
                className="max-h-72 overflow-auto whitespace-pre-wrap bg-slate-50 p-3 text-[11px] leading-relaxed text-slate-700"
              >
                {log || "(暂无日志)"}
              </pre>
            )}
          </div>
        )}

        <PoliciesSection project={project} />
      </div>
    </Modal>
  );
}

const SCOPE_LABEL: Record<string, string> = {
  full: "全仓库",
  recent: "最近改动",
  commit: "指定commit",
};

function PoliciesSection({ project }: { project: Project }) {
  const [policies, setPolicies] = useState<any[]>([]);
  const [name, setName] = useState("最近改动巡检");
  const [scope, setScope] = useState("recent");
  const [scopeArg, setScopeArg] = useState("3");
  const [interval, setIntervalMin] = useState(60);
  const { busy, run } = useSingleFlight();

  const load = useCallback(async () => {
    setPolicies(await api.autopilot.policies(project.id));
  }, [project.id]);
  useEffect(() => {
    load();
  }, [load]);

  // 防抖：同一时刻只跑一个策略动作
  async function guard(fn: () => Promise<void>) {
    await run(fn);
  }

  async function add() {
    if (!name.trim() || busy) return;
    await guard(async () => {
      await api.autopilot.createPolicy({
        project_id: project.id,
        name: name.trim(),
        scope,
        scope_arg: scope === "full" ? null : scopeArg,
        interval_minutes: interval,
      });
      await load();
    });
  }

  return (
    <div className="rounded-lg border border-slate-200 p-3">
      <div className="mb-2 text-sm font-medium text-slate-700">
        定时策略（可配多条：如「最近改动」每小时、「全仓库」每天）
      </div>
      <div className="space-y-1.5">
        {policies.length === 0 && <div className="text-xs text-slate-400">还没有策略，下方添加</div>}
        {policies.map((p) => (
          <div key={p.id} className="flex items-center gap-2 rounded-lg bg-slate-50 px-2.5 py-1.5 text-xs">
            <button
              disabled={busy}
              onClick={() =>
                guard(async () => {
                  await api.autopilot.patchPolicy(p.id, { enabled: !p.enabled });
                  await load();
                })
              }
              className={`h-2 w-2 rounded-full disabled:opacity-50 ${p.enabled ? "bg-emerald-500" : "bg-slate-300"}`}
              title={p.enabled ? "已启用(点击停用)" : "已停用(点击启用)"}
            />
            <span className="font-medium text-slate-700">{p.name}</span>
            <span className="rounded bg-white px-1.5 text-slate-500 ring-1 ring-slate-200">
              {SCOPE_LABEL[p.scope]}
              {p.scope !== "full" ? `·${p.scope_arg}` : ""}
            </span>
            <span className="text-slate-400">每 {p.interval_minutes} 分钟</span>
            {p.enabled && (
              <span className="text-slate-400">· 下次 ~{Math.ceil((p.next_in ?? 0) / 60)}分后</span>
            )}
            <div className="ml-auto flex gap-1.5">
              <button
                className="rounded border border-slate-200 bg-white px-2 py-0.5 text-slate-600 hover:bg-slate-100 disabled:opacity-50"
                disabled={busy}
                onClick={() =>
                  guard(async () => {
                    try {
                      await api.autopilot.runPolicyNow(p.id);
                    } catch (e: any) {
                      toast(e.message, "error");
                    }
                  })
                }
              >
                立即跑
              </button>
              <button
                className="rounded border border-slate-200 bg-white px-2 py-0.5 text-slate-500 hover:bg-rose-50 hover:text-rose-600 disabled:opacity-50"
                disabled={busy}
                onClick={() =>
                  guard(async () => {
                    if (await confirmDialog(`删除策略「${p.name}」？`, { danger: true })) {
                      await api.autopilot.deletePolicy(p.id);
                      await load();
                    }
                  })
                }
              >
                删除
              </button>
            </div>
          </div>
        ))}
      </div>

      <div className="mt-2 flex flex-wrap items-end gap-2 border-t border-slate-100 pt-2 text-xs">
        <input
          className="w-36 rounded-lg border border-slate-200 px-2 py-1"
          placeholder="策略名"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <select
          className="rounded-lg border border-slate-200 bg-white px-2 py-1"
          value={scope}
          onChange={(e) => setScope(e.target.value)}
        >
          <option value="recent">最近改动</option>
          <option value="full">全仓库</option>
          <option value="commit">指定commit</option>
        </select>
        {scope !== "full" && (
          <input
            className="w-16 rounded-lg border border-slate-200 px-2 py-1"
            value={scopeArg}
            onChange={(e) => setScopeArg(e.target.value)}
          />
        )}
        <label className="flex items-center gap-1 text-slate-500">
          每
          <input
            type="number"
            min={5}
            className="w-16 rounded-lg border border-slate-200 px-2 py-1"
            value={interval}
            onChange={(e) => setIntervalMin(Number(e.target.value))}
          />
          分钟
        </label>
        <button
          className="rounded-lg bg-slate-800 px-3 py-1 font-medium text-white hover:bg-slate-700 disabled:opacity-50"
          disabled={busy}
          onClick={add}
        >
          + 添加策略
        </button>
      </div>
    </div>
  );
}
