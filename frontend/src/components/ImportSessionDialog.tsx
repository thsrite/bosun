import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { LocalSession, Project } from "../types";
import { useSingleFlight } from "../useSingleFlight";
import { Modal } from "./Modal";
import { engineName } from "../engines";

function fmtTime(ts: number | null | undefined): string {
  if (!ts) return "时间未知";
  return new Date(ts * 1000).toLocaleString("zh", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function ImportSessionDialog({
  project,
  onClose,
  onDone,
}: {
  project: Project;
  onClose: () => void;
  onDone: () => void;
}) {
  const [bundle, setBundle] = useState<any>(null);
  const [name, setName] = useState("");
  const [msg, setMsg] = useState("");
  const [localSessions, setLocalSessions] = useState<LocalSession[] | null>(null);
  const [localMsg, setLocalMsg] = useState("正在扫描本地会话…");
  const { busy, run } = useSingleFlight();

  const loadLocal = useCallback(async () => {
    setLocalMsg("正在扫描本地会话…");
    try {
      const result = await api.discoverLocalSessions(project.id);
      setLocalSessions(result.sessions);
      setLocalMsg(result.sessions.length ? "" : "没有发现属于本项目的本地会话");
    } catch (err: any) {
      setLocalSessions([]);
      setLocalMsg(`本地会话扫描失败：${err.message}`);
    }
  }, [project.id]);

  useEffect(() => {
    loadLocal();
  }, [loadLocal]);

  async function onFile(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0];
    if (!f) return;
    setName(f.name);
    try {
      const b = JSON.parse(await f.text());
      if (!b.engine || !b.session_uid || !b.jsonl) throw new Error("不是有效的会话 bundle");
      setBundle(b);
      setMsg(`引擎 ${b.engine} · 会话 ${String(b.session_uid).slice(0, 8)}… · ${b.jsonl.length} 字节`);
    } catch (err: any) {
      setBundle(null);
      setMsg(`解析失败：${err.message}`);
    }
  }

  async function submit() {
    await run(async () => {
      if (!bundle) return;
      await api.importSession(project.id, bundle);
      onDone();
      onClose();
    }).catch((e: any) => setMsg(`导入失败：${e.message}`));
  }

  async function attach(s: LocalSession) {
    await run(async () => {
      const result = await api.attachLocalSession(project.id, s.engine, s.session_uid);
      setLocalMsg(
        result.created
          ? `已创建待办任务 #${result.task_id}`
          : `该会话已在任务 #${result.task_id} 中`,
      );
      await loadLocal();
      onDone();
    }).catch((e: any) => setLocalMsg(`加入失败：${e.message}`));
  }

  return (
    <Modal title={`导入 / 发现会话 · ${project.name}`} onClose={onClose} wide>
      <div className="space-y-3 text-sm">
        <div className="space-y-2">
          <div className="flex items-center gap-2">
            <div className="font-medium text-dh-text">本地会话</div>
            <button
              className="ml-auto rounded-lg border border-dh-bsoft px-2.5 py-1 text-xs text-dh-tsoft hover:bg-dh-hover disabled:opacity-50"
              disabled={busy}
              onClick={loadLocal}
            >
              重新扫描
            </button>
          </div>
          {localMsg && <div className="text-xs text-amber-400">{localMsg}</div>}
          <div className="max-h-64 space-y-1.5 overflow-y-auto pr-1">
            {localSessions?.map((s) => (
              <div key={`${s.engine}:${s.session_uid}`} className="rounded-lg border border-dh-bsoft px-3 py-2">
                <div className="flex items-start gap-2">
                  <div className="min-w-0 flex-1">
                    <div className="line-clamp-1 text-sm font-medium text-dh-text" title={s.title}>
                      {s.title}
                    </div>
                    <div className="mt-0.5 flex flex-wrap gap-x-2 gap-y-1 text-[11px] text-slate-400">
                      <span>{engineName(s.engine)}</span>
                      <span className="font-mono">{s.session_uid.slice(0, 8)}</span>
                      <span>{fmtTime(s.updated_at)}</span>
                      <span>{s.turns} 轮</span>
                    </div>
                  </div>
                  {s.task_id ? (
                    <button
                      className="shrink-0 rounded-lg border border-dh-bsoft px-2.5 py-1 text-xs text-slate-400"
                      disabled
                    >
                      已在 #{s.task_id}
                    </button>
                  ) : (
                    <button
                      className="shrink-0 rounded-lg bg-teal-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-teal-500 disabled:opacity-50"
                      disabled={busy}
                      onClick={() => attach(s)}
                    >
                      加入待办
                    </button>
                  )}
                </div>
                {s.prompt && (
                  <div className="mt-1 line-clamp-2 text-xs text-dh-muted" title={s.prompt}>
                    {s.prompt}
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>

        <div className="border-t border-dh-bsoft pt-3">
          <div className="mb-2 font-medium text-dh-text">分享文件</div>
          <p className="mb-2 text-xs text-dh-muted">
            选择别人导出的 <code className="rounded bg-dh-s2 px-1">.bosun.json</code> 会话文件，
            导入后会生成一个可「▶ 继续」的待办任务。
          </p>
          <input
            type="file"
            accept=".json,application/json"
            onChange={onFile}
            className="block w-full text-xs text-dh-tsoft file:mr-3 file:rounded-lg file:border file:border-dh-bsoft file:bg-dh-surface file:px-3 file:py-1.5 file:text-dh-tsoft hover:file:bg-dh-soft"
          />
          {name && <div className="mt-1 text-xs text-slate-400">{name}</div>}
          {msg && <div className="mt-1 text-xs text-amber-400">{msg}</div>}
        </div>

        <div className="flex justify-end gap-2 pt-2">
          <button
            className="rounded-lg border border-dh-bsoft px-3 py-1.5 text-dh-tsoft hover:bg-dh-hover"
            onClick={onClose}
          >
            取消
          </button>
          <button
            className="rounded-lg bg-teal-600 px-3 py-1.5 font-medium text-white hover:bg-teal-500 disabled:opacity-50"
            disabled={busy || !bundle}
            onClick={submit}
          >
            导入
          </button>
        </div>
      </div>
    </Modal>
  );
}
