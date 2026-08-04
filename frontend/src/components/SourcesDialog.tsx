import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { confirmDialog, toast } from "../overlay";
import type { IssueSource, Project } from "../types";
import { useSingleFlight } from "../useSingleFlight";
import { Modal } from "./Modal";

/** 项目问题来源：配置外部 HTTP 源 → 拉取条目进收件箱当 finding。 */

type Cfg = {
  url: string;
  method: string;
  query: string; // JSON 文本
  headers: string; // JSON 文本
  auth_type: string;
  auth_header: string;
  auth_credential: string;
  items_path: string;
  title_field: string;
  detail_field: string;
  severity: string;
  has_credential?: boolean;
};

const EMPTY: Cfg = {
  url: "",
  method: "GET",
  query: "",
  headers: "",
  auth_type: "none",
  auth_header: "X-API-Key",
  auth_credential: "",
  items_path: "",
  title_field: "title",
  detail_field: "body",
  severity: "info",
};

function parseJson(s: string): any {
  const t = s.trim();
  if (!t) return undefined;
  return JSON.parse(t); // 抛错由调用方 catch
}

export function SourcesDialog({
  project,
  onClose,
  onChanged,
}: {
  project: Project;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [sources, setSources] = useState<IssueSource[]>([]);
  const [editing, setEditing] = useState<IssueSource | "new" | null>(null);
  const { busy, run } = useSingleFlight();

  const load = useCallback(async () => {
    setSources(await api.sources.list(project.id));
  }, [project.id]);
  useEffect(() => {
    load();
  }, [load]);

  async function act(fn: () => Promise<unknown>) {
    await run(async () => {
      try {
        await fn();
        await load();
        onChanged();
      } catch (e: any) {
        toast(e.message, "error");
      }
    });
  }

  function ago(ts: number | null): string {
    if (!ts) return "从未拉取";
    const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
    if (s < 60) return `${s} 秒前`;
    if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
    if (s < 86400) return `${Math.floor(s / 3600)} 小时前`;
    return `${Math.floor(s / 86400)} 天前`;
  }

  return (
    <Modal title={`问题来源 · ${project.name}`} onClose={onClose} wide>
      <div className="space-y-3 text-sm">
        {editing === null ? (
          <>
            <div className="flex items-center gap-2">
              <span className="text-xs text-slate-400">
                外部 API 拉取的条目会进「问题收件箱」当 finding，你再勾选转任务
              </span>
              <button
                className="ml-auto shrink-0 rounded-lg bg-teal-600 px-3 py-1.5 font-medium text-white hover:bg-teal-700"
                onClick={() => setEditing("new")}
              >
                ＋ 新增来源
              </button>
            </div>
            <div className="space-y-1.5">
              {sources.length === 0 && (
                <div className="py-6 text-center text-xs text-slate-400">还没有来源，点右上新增</div>
              )}
              {sources.map((s) => (
                <div key={s.id} className="flex items-center gap-2 rounded-lg border border-slate-200 px-3 py-2">
                  <span className="font-medium text-slate-800">{s.name}</span>
                  <span className="rounded bg-slate-100 px-1.5 text-[10px] uppercase text-slate-500">{s.type}</span>
                  {!s.enabled && <span className="text-[10px] text-slate-400">已停用</span>}
                  <span className="truncate font-mono text-[11px] text-slate-400">{s.config.url}</span>
                  <span className="ml-auto shrink-0 text-[11px] text-slate-400">{ago(s.last_pull_at)}</span>
                  <div className="flex shrink-0 gap-1.5">
                    <button
                      className="rounded-lg bg-violet-500 px-2 py-0.5 text-xs text-white hover:bg-violet-600 disabled:opacity-50"
                      disabled={busy}
                      onClick={() =>
                        act(async () => {
                          const r = await api.sources.pull(s.id);
                          toast(`拉取到 ${r.new_findings} 条新问题`, "success");
                        })
                      }
                    >
                      拉取
                    </button>
                    <button
                      className="rounded-lg border border-slate-200 px-2 py-0.5 text-xs text-slate-600 hover:bg-slate-50"
                      onClick={() => setEditing(s)}
                    >
                      编辑
                    </button>
                    <button
                      className="rounded-lg border border-slate-200 px-2 py-0.5 text-xs text-slate-500 hover:bg-rose-50 hover:text-rose-600 disabled:opacity-50"
                      disabled={busy}
                      onClick={() =>
                        act(async () => {
                          if (await confirmDialog(`删除来源「${s.name}」？`, { danger: true })) {
                            await api.sources.remove(s.id);
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
          </>
        ) : (
          <SourceForm
            project={project}
            source={editing === "new" ? null : editing}
            onCancel={() => setEditing(null)}
            onSaved={async () => {
              setEditing(null);
              await load();
              onChanged();
            }}
          />
        )}
      </div>
    </Modal>
  );
}

function SourceForm({
  project,
  source,
  onCancel,
  onSaved,
}: {
  project: Project;
  source: IssueSource | null;
  onCancel: () => void;
  onSaved: () => void;
}) {
  const [name, setName] = useState(source?.name ?? "");
  const [enabled, setEnabled] = useState(source?.enabled ?? true);
  const c = source?.config ?? {};
  const [cfg, setCfg] = useState<Cfg>({
    ...EMPTY,
    url: c.url ?? "",
    method: c.method ?? "GET",
    query: c.query ? JSON.stringify(c.query) : "",
    headers: c.headers ? JSON.stringify(c.headers) : "",
    auth_type: c.auth_type ?? "none",
    auth_header: c.auth_header ?? "X-API-Key",
    auth_credential: "",
    items_path: c.items_path ?? "",
    title_field: c.title_field ?? "title",
    detail_field: c.detail_field ?? "body",
    severity: c.severity ?? "info",
    has_credential: c.has_credential,
  });
  const { busy, run: runOnce } = useSingleFlight();
  const [test, setTest] = useState<{ count: number; sample: { title: string }[] } | null>(null);

  const set = (k: keyof Cfg, v: string) => setCfg((p) => ({ ...p, [k]: v }));

  function buildConfig(): any {
    let query: any, headers: any;
    try {
      query = parseJson(cfg.query);
      headers = parseJson(cfg.headers);
    } catch {
      throw new Error("query / headers 不是合法 JSON");
    }
    return {
      url: cfg.url.trim(),
      method: cfg.method,
      query,
      headers,
      auth_type: cfg.auth_type,
      auth_header: cfg.auth_header,
      auth_credential: cfg.auth_credential, // 空=后端保留原值
      items_path: cfg.items_path.trim(),
      title_field: cfg.title_field.trim() || "title",
      detail_field: cfg.detail_field.trim(),
      severity: cfg.severity,
    };
  }

  async function run(fn: (config: any) => Promise<void>) {
    await runOnce(async () => {
      try {
        await fn(buildConfig());
      } catch (e: any) {
        toast(e.message, "error");
      }
    });
  }

  return (
    <div className="space-y-2.5">
      <div className="flex items-center gap-2">
        <input
          className="flex-1 rounded-lg border border-slate-200 px-2.5 py-1.5"
          autoComplete="off"
          placeholder="来源名称（会作为 finding 的来源标识 ext:名称）"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <label className="flex shrink-0 items-center gap-1.5 text-xs text-slate-500">
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
          启用
        </label>
      </div>

      <div className="flex gap-2">
        <select
          className="shrink-0"
          value={cfg.method}
          onChange={(e) => set("method", e.target.value)}
        >
          <option>GET</option>
          <option>POST</option>
        </select>
        <input
          className="flex-1 rounded-lg border border-slate-200 px-2.5 py-1.5 font-mono text-xs"
          autoComplete="off"
          placeholder="https://api.example.com/issues"
          value={cfg.url}
          onChange={(e) => set("url", e.target.value)}
        />
      </div>

      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        <Field label="query（JSON，可空）">
          <input className="in" placeholder='{"state":"open"}' value={cfg.query} onChange={(e) => set("query", e.target.value)} />
        </Field>
        <Field label="headers（JSON，可空）">
          <input className="in" placeholder='{"X-Foo":"bar"}' value={cfg.headers} onChange={(e) => set("headers", e.target.value)} />
        </Field>
      </div>

      <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
        <Field label="授权方式">
          <select value={cfg.auth_type} onChange={(e) => set("auth_type", e.target.value)}>
            <option value="none">无</option>
            <option value="bearer">Bearer Token</option>
            <option value="api_key">API Key（头）</option>
            <option value="basic">Basic（user:pass）</option>
          </select>
        </Field>
        {cfg.auth_type === "api_key" && (
          <Field label="头名">
            <input className="in" value={cfg.auth_header} onChange={(e) => set("auth_header", e.target.value)} />
          </Field>
        )}
        {cfg.auth_type !== "none" && (
          <Field label={cfg.has_credential ? "凭据（留空=不变）" : "凭据"}>
            <input
              className="in"
              type="password"
              autoComplete="off"
              data-1p-ignore
              data-lpignore="true"
              name="bosun-source-credential"
              placeholder={cfg.has_credential ? "已保存 ••••" : cfg.auth_type === "basic" ? "user:pass" : "token / key"}
              value={cfg.auth_credential}
              onChange={(e) => set("auth_credential", e.target.value)}
            />
          </Field>
        )}
      </div>

      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Field label="items_path">
          <input className="in" placeholder="data.issues（空=根）" value={cfg.items_path} onChange={(e) => set("items_path", e.target.value)} />
        </Field>
        <Field label="title 字段">
          <input className="in" value={cfg.title_field} onChange={(e) => set("title_field", e.target.value)} />
        </Field>
        <Field label="detail 字段">
          <input className="in" value={cfg.detail_field} onChange={(e) => set("detail_field", e.target.value)} />
        </Field>
        <Field label="严重度">
          <select value={cfg.severity} onChange={(e) => set("severity", e.target.value)}>
            <option value="info">info</option>
            <option value="warning">warning</option>
            <option value="error">error</option>
          </select>
        </Field>
      </div>

      {test && (
        <div className="rounded-lg bg-slate-50 p-2 text-xs text-slate-600">
          测到 {test.count} 条{test.count > 0 && "，样例："}
          {test.sample.map((s, i) => (
            <div key={i} className="truncate text-slate-500">
              · {s.title}
            </div>
          ))}
        </div>
      )}

      <div className="flex items-center gap-2 pt-1">
        <button
          className="rounded-lg border border-slate-200 px-3 py-1.5 text-slate-600 hover:bg-slate-50 disabled:opacity-50"
          disabled={busy || !cfg.url.trim()}
          onClick={() =>
            run(async (config) => {
              const r = await api.sources.test(project.id, { type: "http", config });
              setTest(r);
              toast(`测试成功，拉到 ${r.count} 条`, "success");
            })
          }
        >
          测试连接
        </button>
        <div className="ml-auto flex gap-2">
          <button className="rounded-lg border border-slate-200 px-3 py-1.5 text-slate-600 hover:bg-slate-50" onClick={onCancel}>
            取消
          </button>
          <button
            className="rounded-lg bg-teal-600 px-3 py-1.5 font-medium text-white hover:bg-teal-700 disabled:opacity-50"
            disabled={busy || !name.trim() || !cfg.url.trim()}
            onClick={() =>
              run(async (config) => {
                const body = { name: name.trim(), type: "http", enabled, config };
                if (source) await api.sources.update(source.id, body);
                else await api.sources.create(project.id, body);
                onSaved();
              })
            }
          >
            保存
          </button>
        </div>
      </div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-[11px] text-slate-500">{label}</span>
      {children}
    </label>
  );
}
