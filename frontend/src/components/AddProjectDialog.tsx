import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { useSingleFlight } from "../useSingleFlight";
import { Modal } from "./Modal";

type Entry = { name: string; path: string; is_git: boolean };

export function AddProjectDialog({
  onClose,
  onDone,
}: {
  onClose: () => void;
  onDone: () => void;
}) {
  const [mode, setMode] = useState<"add" | "scan">("scan");
  const [cur, setCur] = useState("");
  const [pathInput, setPathInput] = useState("");
  const [parent, setParent] = useState<string | null>(null);
  const [entries, setEntries] = useState<Entry[]>([]);
  const [loading, setLoading] = useState(false);
  const { busy, run } = useSingleFlight();
  const [msg, setMsg] = useState("");

  const browse = useCallback(async (path?: string) => {
    setLoading(true);
    setMsg("");
    try {
      const r = await api.browse(path);
      setCur(r.path);
      setPathInput(r.path);
      setParent(r.parent);
      setEntries(r.entries);
    } catch (e: any) {
      setMsg(`打不开：${e.message}`);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    browse(); // 默认从 home 开始
  }, [browse]);

  async function submit() {
    await run(async () => {
      if (!cur) return;
      setMsg("");
      if (mode === "scan") {
        const r = await api.scan(cur);
        setMsg(`扫描到并导入 ${r.count} 个项目`);
        onDone();
      } else {
        await api.addProject(cur);
        onDone();
        onClose();
      }
    }).catch((e: any) => setMsg(`失败: ${e.message}`));
  }

  return (
    <Modal title="添加 / 扫描路径" onClose={onClose} wide>
      <div className="space-y-3 text-sm">
        <div className="flex flex-wrap gap-x-4 gap-y-1.5">
          <label className="flex items-center gap-1.5">
            <input type="radio" checked={mode === "scan"} onChange={() => setMode("scan")} />
            扫描根目录（批量导入其下项目）
          </label>
          <label className="flex items-center gap-1.5">
            <input type="radio" checked={mode === "add"} onChange={() => setMode("add")} />
            添加当前目录（单个项目）
          </label>
        </div>

        {/* 路径栏：可输入跳转 + 上级 */}
        <div className="flex items-center gap-2">
          <button
            className="shrink-0 rounded-lg border border-dh-bsoft px-2.5 py-2 text-dh-tsoft hover:bg-dh-hover disabled:opacity-40"
            disabled={!parent || loading}
            onClick={() => parent && browse(parent)}
            title="上级目录"
          >
            ↑
          </button>
          <input
            className="min-w-0 flex-1 rounded-lg border border-dh-bsoft bg-dh-soft px-2.5 py-2 font-mono text-xs text-dh-text focus:border-dh-m2 focus:outline-none"
            value={pathInput}
            onChange={(e) => setPathInput(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && browse(pathInput.trim())}
            placeholder="输入或粘贴路径后回车跳转"
          />
        </div>

        {/* 目录列表 */}
        <div className="h-64 overflow-auto rounded-lg border border-dh-bsoft">
          {loading ? (
            <div className="p-4 text-center text-xs text-slate-400">读取中…</div>
          ) : entries.length === 0 ? (
            <div className="p-4 text-center text-xs text-slate-400">（此目录下无子文件夹）</div>
          ) : (
            entries.map((e) => (
              <button
                key={e.path}
                onClick={() => browse(e.path)}
                className="flex w-full items-center gap-2 border-b border-dh-bsoft px-3 py-1.5 text-left last:border-0 hover:bg-dh-hover"
              >
                <span className="shrink-0">{e.is_git ? "⚓" : "📁"}</span>
                <span className="min-w-0 flex-1 truncate text-dh-tsoft">{e.name}</span>
                <span className="shrink-0 text-slate-300">›</span>
              </button>
            ))
          )}
        </div>

        {msg && <div className="text-xs text-amber-400">{msg}</div>}

        <div className="flex items-center gap-2 pt-1">
          <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-slate-400">
            当前：{cur || "…"}
          </span>
          <button
            className="shrink-0 rounded-lg border border-dh-bsoft px-3 py-1.5 text-dh-tsoft hover:bg-dh-hover"
            onClick={onClose}
          >
            关闭
          </button>
          <button
            className="shrink-0 rounded-lg bg-teal-600 px-3 py-1.5 font-medium text-white hover:bg-teal-500 disabled:opacity-50"
            disabled={busy || loading || !cur}
            onClick={submit}
          >
            {mode === "scan" ? "扫描导入此目录" : "添加此目录"}
          </button>
        </div>
      </div>
    </Modal>
  );
}
