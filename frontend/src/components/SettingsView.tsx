import { useEffect, useRef, useState, type FormEvent } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api, type AppSettings, type AuthStatus, type EngineToolInfo, type SelfUpdateInfo, type SelfUpdateResult, type StorageInfo } from "../api";
import { setToken } from "../auth";
import { engineName } from "../engines";
import { useEngineVisible } from "../installedEngines";
import type { Engine } from "../types";
import { confirmDialog, toast } from "../overlay";

type ModelOption = { value: string; label: string };

function readDetail(err: unknown): string {
  const raw = err instanceof Error ? err.message : String(err);
  try {
    return JSON.parse(raw).detail || raw;
  } catch {
    return raw;
  }
}

function ModelCombobox({
  id,
  value,
  options,
  onChange,
  onCommit,
}: {
  id: string;
  value: string;
  options: ModelOption[];
  onChange: (value: string) => void;
  onCommit: (value: string) => void;
}) {
  const rootRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const [open, setOpen] = useState(false);
  const [filtering, setFiltering] = useState(false);
  const [highlighted, setHighlighted] = useState(-1);
  const query = filtering ? value.trim().toLocaleLowerCase() : "";
  const selectedLabel = value ? options.find((option) => option.value === value)?.label : undefined;
  const displayValue = filtering ? value : selectedLabel ?? value;
  const filteredOptions = options.filter((option) => {
    if (!query) return true;
    return option.value.toLocaleLowerCase().includes(query) || option.label.toLocaleLowerCase().includes(query);
  });

  useEffect(() => {
    if (!open) return;
    const closeOnOutsidePointer = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", closeOnOutsidePointer);
    return () => document.removeEventListener("pointerdown", closeOnOutsidePointer);
  }, [open]);

  function selectOption(next: string) {
    onChange(next);
    onCommit(next);
    setOpen(false);
    setFiltering(false);
    setHighlighted(-1);
    inputRef.current?.focus({ preventScroll: true });
  }

  return (
    <div ref={rootRef} className="relative w-44">
      <input
        ref={inputRef}
        id={id}
        role="combobox"
        aria-autocomplete="list"
        aria-expanded={open}
        aria-controls={`${id}-options`}
        aria-activedescendant={highlighted >= 0 ? `${id}-option-${highlighted}` : undefined}
        className="w-full rounded-md border border-dh-bsoft bg-dh-surface py-1 pl-2 pr-7 text-sm text-dh-text outline-none transition focus:border-teal-400 focus:ring-1 focus:ring-teal-400/30"
        value={displayValue}
        placeholder="默认 / 自定义 ID"
        onFocus={() => {
          setOpen(true);
          setFiltering(false);
        }}
        onChange={(event) => {
          onChange(event.target.value);
          setOpen(true);
          setFiltering(true);
          setHighlighted(-1);
        }}
        onBlur={() => {
          onCommit(value);
          window.setTimeout(() => {
            if (!rootRef.current?.contains(document.activeElement)) {
              setOpen(false);
              setFiltering(false);
            }
          }, 0);
        }}
        onKeyDown={(event) => {
          if (event.key === "ArrowDown") {
            event.preventDefault();
            setOpen(true);
            if (!open) setFiltering(false);
            setHighlighted((current) => Math.min(current + 1, filteredOptions.length - 1));
          } else if (event.key === "ArrowUp") {
            event.preventDefault();
            setHighlighted((current) => Math.max(current - 1, 0));
          } else if (event.key === "Enter") {
            event.preventDefault();
            if (open && highlighted >= 0 && filteredOptions[highlighted]) {
              selectOption(filteredOptions[highlighted].value);
            } else {
              onCommit(value);
              setOpen(false);
              setFiltering(false);
              event.currentTarget.blur();
            }
          } else if (event.key === "Escape") {
            event.preventDefault();
            setOpen(false);
            setFiltering(false);
          }
        }}
      />
      <button
        type="button"
        tabIndex={-1}
        className="absolute inset-y-0 right-0 grid w-7 place-items-center rounded-r-md text-[10px] text-slate-400 hover:bg-dh-hover hover:text-slate-50"
        aria-label={open ? "收起模型选项" : "展开模型选项"}
        onMouseDown={(event) => event.preventDefault()}
        onClick={() => {
          setOpen((current) => !current);
          setFiltering(false);
          setHighlighted(-1);
          inputRef.current?.focus({ preventScroll: true });
        }}
      >
        <span className={`transition-transform ${open ? "rotate-180" : ""}`}>▼</span>
      </button>
      {open && (
        <div
          id={`${id}-options`}
          role="listbox"
          className="absolute left-0 top-[calc(100%+0.3rem)] z-[80] max-h-56 min-w-full overflow-auto rounded-lg border border-dh-bsoft bg-dh-surface p-1 shadow-xl shadow-slate-900/15"
        >
          {filteredOptions.length > 0 ? filteredOptions.map((option, index) => {
            const selected = option.value === value;
            const active = index === highlighted;
            return (
              <button
                key={option.value || "default"}
                id={`${id}-option-${index}`}
                type="button"
                role="option"
                aria-selected={selected}
                className={`flex w-full items-center justify-between gap-3 rounded-md px-2.5 py-1.5 text-left text-xs ${
                  selected
                    ? "bg-slate-400/10 font-medium text-dh-tsoft"
                    : active
                      ? "bg-dh-s2 text-dh-text"
                      : "text-dh-tsoft hover:bg-dh-hover"
                }`}
                onMouseEnter={() => setHighlighted(index)}
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => selectOption(option.value)}
              >
                <span className="whitespace-nowrap">{option.label}</span>
                {selected && <span className="text-teal-500">✓</span>}
              </button>
            );
          }) : (
            <div className="px-2.5 py-2 text-xs text-slate-400">按 Enter 使用自定义模型 ID</div>
          )}
        </div>
      )}
    </div>
  );
}

function Section({ title, hint, aside, children }: { title: string; hint?: string; aside?: React.ReactNode; children: React.ReactNode }) {
  return (
    <section className="card h-full p-4 lg:p-5">
      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
        <h2 className="text-sm font-semibold text-dh-text">{title}</h2>
        {aside}
      </div>
      {hint && <p className="mt-1 text-xs text-slate-400">{hint}</p>}
      <div className="mt-4 space-y-4">{children}</div>
    </section>
  );
}

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1.5">
      <div className="min-w-0">
        <div className="text-sm text-dh-tsoft">{label}</div>
        {hint && <div className="text-[11px] text-slate-400">{hint}</div>}
      </div>
      {children}
    </div>
  );
}

/** Bosun 版本：对比 GitHub 最新 release，并在本地 git 工作区上一键更新。 */
function BosunVersion() {
  const [info, setInfo] = useState<SelfUpdateInfo | null>(null);
  const [checking, setChecking] = useState(false);
  const [updating, setUpdating] = useState(false);
  const [result, setResult] = useState<SelfUpdateResult | null>(null);

  useEffect(() => {
    let alive = true;
    api.selfUpdate
      .status()
      .then((next) => { if (alive) setInfo(next); })
      .catch(() => {});
    return () => { alive = false; };
  }, []);

  async function check() {
    if (checking) return;
    setChecking(true);
    setResult(null);
    try {
      const next = await api.selfUpdate.check();
      setInfo(next);
      if (next.check_error) toast(next.check_error, "error");
      else if (next.update_available) toast(`发现新版本 ${next.latest_tag}`, "success");
      else toast("已是最新版本", "success");
    } catch (err) {
      toast(`检查更新失败：${readDetail(err)}`, "error");
    } finally {
      setChecking(false);
    }
  }

  async function update() {
    if (updating || !info) return;
    const confirmed = await confirmDialog(
      `将本地代码更新到 ${info.latest_tag}，并按需重装依赖、重建前端，完成后后端会重启（运行中的任务会被中断）。确定更新？`,
      { danger: true },
    );
    if (!confirmed) return;

    setUpdating(true);
    setResult(null);
    try {
      const next = await api.selfUpdate.run();
      setResult(next);
      if (!next.ok) toast(`更新失败：${next.error || "未知错误"}`, "error");
      else if (next.restart === "manual") toast(next.restart_hint || "更新完成，请手动重启后端", "success");
      else toast(next.message || "更新完成，后端正在重启", "success");
      if (next.ok) setInfo(await api.selfUpdate.status().catch(() => info));
    } catch (err) {
      toast(`更新失败：${readDetail(err)}`, "error");
    } finally {
      setUpdating(false);
    }
  }

  const versionText = info
    ? `v${info.current_version}${info.branch ? ` · ${info.branch}` : ""}${info.head ? ` @ ${info.head}` : ""}`
    : "读取中…";

  return (
    <Section title="Bosun 版本" hint={`更新以 GitHub Release 为准，从 ${info?.repo || "thsrite/bosun"} 拉取。`}>
      <Field label="当前版本" hint={versionText}>
        <button
          type="button"
          disabled={checking || updating}
          onClick={() => void check()}
          className="rounded-lg border border-dh-bsoft px-3 py-1.5 text-sm text-dh-tsoft hover:bg-dh-hover disabled:cursor-not-allowed disabled:opacity-50"
        >{checking ? "检查中…" : "检查更新"}</button>
      </Field>

      {info?.check_error && <div className="text-xs text-rose-500">{info.check_error}</div>}

      {info?.update_available && (
        <Field
          label={`新版本 ${info.latest_tag}`}
          hint={info.published_at ? `发布于 ${new Date(info.published_at).toLocaleString()}` : undefined}
        >
          <div className="flex items-center gap-2">
            <a
              href={info.release_url || info.releases_url}
              target="_blank"
              rel="noreferrer"
              className="text-xs text-dh-tsoft hover:underline"
            >更新说明</a>
            <button
              type="button"
              disabled={updating || !info.can_update}
              onClick={() => void update()}
              className="rounded-lg border border-slate-400/40 px-3 py-1.5 text-sm text-dh-tsoft hover:bg-slate-400/20 disabled:cursor-not-allowed disabled:opacity-50"
            >{updating ? "更新中…" : "立即更新"}</button>
          </div>
        </Field>
      )}

      {info?.update_available && info.release_notes && (
        <div className="chat-md max-h-40 overflow-auto rounded-lg border border-dh-bsoft bg-dh-soft px-3 py-2">
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            components={{ a: (p) => <a {...p} target="_blank" rel="noreferrer" /> }}
          >
            {info.release_notes}
          </ReactMarkdown>
        </div>
      )}

      {info && info.blockers.length > 0 && (
        <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-400">
          无法在线更新：{info.blockers.join("；")}
        </div>
      )}

      {result && (
        <div className="space-y-1 rounded-lg border border-dh-bsoft bg-dh-soft px-3 py-2 text-[11px] text-dh-tsoft">
          {result.steps.map((step, index) => (
            <div key={`${step.name}-${index}`} className="flex gap-2">
              <span className={step.skipped ? "text-slate-400" : step.ok ? "text-dh-tsoft" : "text-rose-500"}>
                {step.skipped ? "–" : step.ok ? "✓" : "✕"}
              </span>
              <span className="min-w-0 flex-1">
                {step.name}
                {step.skipped && step.output ? `（${step.output}）` : ""}
                {!step.ok && step.output ? <pre className="mt-1 whitespace-pre-wrap text-rose-500">{step.output}</pre> : null}
              </span>
            </div>
          ))}
          {result.error && <div className="text-rose-500">{result.error}</div>}
        </div>
      )}
    </Section>
  );
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

/** 数据与存储：数据目录、数据库与日志的路径和占用空间，只读展示。 */
function StorageOverview() {
  const [info, setInfo] = useState<StorageInfo | null>(null);
  const [loading, setLoading] = useState(false);
  const [cleaning, setCleaning] = useState(false);

  async function load() {
    if (loading) return;
    setLoading(true);
    try {
      setInfo(await api.getStorageInfo());
    } catch (err) {
      toast(`读取存储信息失败：${readDetail(err)}`, "error");
    } finally {
      setLoading(false);
    }
  }

  async function compress() {
    if (cleaning) return;
    const confirmed = await confirmDialog(
      "将把已结束任务（完成 / 失败 / 已取消）的历史日志压缩为 .gz 归档；压缩后仍可正常查看（自动从压缩包读取），进行中和可恢复的任务不受影响。确定压缩？",
    );
    if (!confirmed) return;
    setCleaning(true);
    try {
      const result = await api.compressStorage();
      setInfo(result);
      toast(`已压缩 ${result.compressed_count} 个日志文件，节省 ${formatBytes(result.saved_size)}`, "success");
    } catch (err) {
      toast(`压缩失败：${readDetail(err)}`, "error");
    } finally {
      setCleaning(false);
    }
  }

  useEffect(() => {
    let alive = true;
    api.getStorageInfo()
      .then((next) => { if (alive) setInfo(next); })
      .catch(() => {});
    return () => { alive = false; };
  }, []);

  return (
    <Section
      title="数据与存储"
      hint="Bosun 的配置和数据都存放在数据目录下，可用 BOSUN_DATA 环境变量迁移。"
      aside={
        <button
          type="button"
          disabled={loading}
          onClick={() => void load()}
          className="rounded-md border border-dh-bsoft bg-dh-surface px-3 py-1 text-sm text-dh-tsoft transition hover:border-teal-400 hover:text-dh-text disabled:cursor-not-allowed disabled:opacity-50"
        >{loading ? "统计中…" : "刷新"}</button>
      }
    >
      {!info ? (
        <p className="text-xs text-slate-400">读取中…</p>
      ) : (
        <>
          <Field label="数据目录" hint={info.data_dir}>
            <span className="text-sm text-dh-text">{formatBytes(info.total_size)}</span>
          </Field>
          <Field label="数据库" hint={`${info.db_path}（含 -wal/-shm，任务与设置都存在这里）`}>
            <span className="text-sm text-dh-text">{formatBytes(info.db_size)}</span>
          </Field>
          <Field
            label="任务日志"
            hint={`${info.log_dir} · ${info.log_count} 个文件${info.archived_count > 0 ? `（含 ${info.archived_count} 个压缩包）` : ""}`}
          >
            <div className="flex items-center gap-2">
              <span className="text-sm text-dh-text">{formatBytes(info.log_size)}</span>
              <button
                type="button"
                disabled={cleaning}
                onClick={() => void compress()}
                className="rounded-lg border border-dh-bsoft px-3 py-1.5 text-sm text-dh-tsoft hover:bg-dh-hover disabled:cursor-not-allowed disabled:opacity-50"
              >{cleaning ? "压缩中…" : "压缩历史日志"}</button>
            </div>
          </Field>
          <Field label="其它文件" hint="后端 / 前端运行日志等">
            <span className="text-sm text-dh-text">{formatBytes(info.other_size)}</span>
          </Field>
        </>
      )}
    </Section>
  );
}

/** 访问控制：设置 / 修改 / 关闭访问口令，以及退出登录。 */
function AccessControl({ auth, onAuthChanged }: { auth: AuthStatus; onAuthChanged: () => void }) {
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const envManaged = auth.source === "env";

  async function savePassword(e: FormEvent) {
    e.preventDefault();
    if (saving) return;
    if (newPassword !== confirmPassword) {
      setError("两次输入的口令不一致");
      return;
    }
    if (newPassword.trim().length < auth.min_password_length) {
      setError(`口令至少 ${auth.min_password_length} 位`);
      return;
    }
    setSaving(true);
    setError("");
    try {
      const res = await api.auth.setPassword(newPassword, currentPassword);
      // 改密会踢掉所有旧会话，后端已下发新 token，换上它免得自己被踢出去
      setToken(res.token);
      setCurrentPassword("");
      setNewPassword("");
      setConfirmPassword("");
      toast(auth.enabled ? "口令已更新，其它设备需重新登录" : "登录已启用", "success");
      onAuthChanged();
    } catch (err) {
      setError(readDetail(err));
    } finally {
      setSaving(false);
    }
  }

  async function disableLogin() {
    const ok = await confirmDialog(
      "关闭登录后，任何能访问本服务的人都可以直接操作终端和任务。确定关闭？",
      { danger: true },
    );
    if (!ok) return;
    try {
      await api.auth.disablePassword();
      setToken(null);
      toast("登录已关闭", "success");
      onAuthChanged();
    } catch (err) {
      toast(`关闭失败：${readDetail(err)}`, "error");
    }
  }

  async function logout() {
    try {
      await api.auth.logout();
    } finally {
      setToken(null);
    }
  }

  return (
    <Section
      title="访问控制"
      hint="口令用于阻止他人打开工作台执行任务；终端 WebSocket 同样校验。"
    >
      {!auth.enabled && (
        <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-400">
          ⚠️ 当前未启用登录，任何能访问本服务的人都能操作终端与任务。若本机以外可访问，请立刻设置口令。
        </div>
      )}
      {envManaged && (
        <div className="rounded-lg border border-dh-bsoft bg-dh-soft px-3 py-2 text-xs text-dh-muted">
          口令由环境变量 <code>BOSUN_PASSWORD</code> 提供，需改环境变量并重启后端。
        </div>
      )}

      {!envManaged && (
        <form onSubmit={savePassword} className="space-y-3">
          {auth.enabled && (
            <Field label="当前口令">
              <input
                type="password"
                autoComplete="current-password"
                className="w-52 rounded-md border border-dh-bsoft bg-dh-surface px-2.5 py-1 text-sm text-dh-text"
                value={currentPassword}
                onChange={(e) => setCurrentPassword(e.target.value)}
              />
            </Field>
          )}
          <Field label={auth.enabled ? "新口令" : "设置口令"} hint={`至少 ${auth.min_password_length} 位`}>
            <input
              type="password"
              autoComplete="new-password"
              className="w-52 rounded-md border border-dh-bsoft bg-dh-surface px-2.5 py-1 text-sm text-dh-text"
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
            />
          </Field>
          <Field label="确认口令">
            <input
              type="password"
              autoComplete="new-password"
              className="w-52 rounded-md border border-dh-bsoft bg-dh-surface px-2.5 py-1 text-sm text-dh-text"
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
            />
          </Field>
          <div className="flex items-center justify-between gap-3 pt-1">
            <div className="text-xs text-rose-500">{error}</div>
            <button
              type="submit"
              disabled={saving || !newPassword}
              className="shrink-0 rounded-lg bg-teal-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-teal-500 disabled:opacity-50"
            >
              {saving ? "保存中…" : auth.enabled ? "修改口令" : "启用登录"}
            </button>
          </div>
        </form>
      )}

      {auth.enabled && (
        <div className="space-y-3 border-t border-dh-bsoft pt-4">
          <Field label="当前会话" hint="仅退出本设备的登录态，其它设备不受影响">
            <button
              type="button"
              onClick={logout}
              className="rounded-lg border border-dh-bsoft px-3 py-1.5 text-sm text-dh-tsoft hover:bg-dh-hover"
            >
              退出登录
            </button>
          </Field>
          {!envManaged && (
            <Field label="口令保护" hint="关闭后任何人都能直接打开工作台">
              <button
                type="button"
                onClick={disableLogin}
                className="rounded-lg border border-rose-500/40 px-3 py-1.5 text-sm text-rose-500 hover:bg-rose-500/20"
              >
                关闭登录
              </button>
            </Field>
          )}
        </div>
      )}
    </Section>
  );
}

/** 设置分组：小节标题 + 桌面端两列网格(默认 stretch，配合卡片 h-full 同行等高)。 */
function SettingsGroup({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <section className="space-y-3">
      <h3 className="px-1 text-[11px] font-semibold uppercase tracking-wider text-dh-muted">{label}</h3>
      <div className="grid gap-4 lg:grid-cols-2">{children}</div>
    </section>
  );
}

/** 引擎卡头上的安装状态胶囊：已安装显示版本号，探测未回来时不占位。 */
function EngineVersionChip({ tool }: { tool: EngineToolInfo | undefined }) {
  if (!tool?.installed) return null;
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full border border-dh-bsoft bg-dh-surface px-2 py-0.5 text-[11px] text-dh-tsoft">
      <span className="h-1.5 w-1.5 rounded-full bg-teal-500/80" />
      {tool.version ? `v${tool.version}` : "已安装"}
    </span>
  );
}

const ENGINE_INTRO: Record<Engine, { blurb: string; install: string }> = {
  cc: { blurb: "Anthropic 的 Claude Code CLI", install: "npm i -g @anthropic-ai/claude-code" },
  codex: { blurb: "OpenAI 的 Codex CLI", install: "npm i -g @openai/codex" },
  omp: { blurb: "Oh My Pi，自带 provider 凭据", install: "npm i -g @oh-my-pi/pi-coding-agent" },
  kimi: { blurb: "Moonshot 的 Kimi Code CLI，自带 provider 凭据", install: "npm i -g @moonshot-ai/kimi-code" },
};

/** 未安装引擎的灰态摘要卡。其余界面对未安装引擎一律隐藏，
 * 只在设置页保留一张占位卡，成对展示「支持什么」和「怎么装」。 */
function UninstalledEngineCard({ engine }: { engine: Engine }) {
  const intro = ENGINE_INTRO[engine];
  return (
    <section className="card h-full border-dashed p-4 opacity-70 lg:p-5">
      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1.5">
        <div className="flex items-center gap-2">
          <h2 className="text-sm font-semibold text-dh-tsoft">{engineName(engine)}</h2>
          <span className="rounded-full border border-dh-bsoft px-2 py-0.5 text-[11px] text-dh-muted">未安装</span>
        </div>
        <code className="rounded-md bg-dh-s2 px-2 py-1 font-mono text-[11px] text-dh-muted">{intro.install}</code>
      </div>
      <p className="mt-1.5 text-xs text-slate-400">{intro.blurb}；装好后这里会出现它的模型设置，其它界面才会显示该引擎。</p>
    </section>
  );
}

/** 设置页：运行参数（并发 / 引擎模型 / 推理档位）与访问控制。 */
export function SettingsView({
  settings,
  onChange,
  onSettingsPatch,
  auth,
  onAuthChanged,
}: {
  settings: AppSettings;
  onChange: (patch: Partial<AppSettings>) => Promise<void>;
  onSettingsPatch: (patch: Partial<AppSettings>) => void;
  auth: AuthStatus;
  onAuthChanged: () => void;
}) {
  const [refreshingModels, setRefreshingModels] = useState({
    cc: false,
    codex: false,
    kimi: false,
  });
  const [restartingBackend, setRestartingBackend] = useState(false);
  // 没装的引擎不渲染设置表单，降级为灰态占位卡
  const showClaude = useEngineVisible("cc");
  const showCodex = useEngineVisible("codex");
  const showOmp = useEngineVisible("omp");
  const showKimi = useEngineVisible("kimi");
  // 引擎卡头的版本胶囊数据；探测失败不阻塞页面，胶囊留空即可
  const [engineTools, setEngineTools] = useState<Record<string, EngineToolInfo> | null>(null);
  useEffect(() => {
    let alive = true;
    api.engineTools
      .list()
      .then((tools) => { if (alive) setEngineTools(tools); })
      .catch(() => {});
    return () => { alive = false; };
  }, []);
  // omp 没有可消费的模型目录接口，设置页直接填模型 ID；kimi 的刷新读的是
  // ~/.kimi-code/config.toml 里的模型别名。
  async function refreshModels(engine: "cc" | "codex" | "kimi") {
    if (refreshingModels[engine]) return;

    const engineLabel = engine === "cc" ? "Claude" : engine === "codex" ? "Codex" : "Kimi";
    setRefreshingModels((current) => ({ ...current, [engine]: true }));
    try {
      const result = await api.refreshModelOptions(engine);
      if (engine === "cc") {
        onSettingsPatch({ claude_model_options: result.model_options });
      } else if (engine === "codex") {
        onSettingsPatch({ codex_model_options: result.model_options });
      } else {
        onSettingsPatch({ kimi_model_options: result.model_options });
      }
      toast(`${engineLabel} 模型列表已刷新`, "success");
    } catch (err) {
      toast(`刷新 ${engineLabel} 模型列表失败：${readDetail(err)}`, "error");
    } finally {
      setRefreshingModels((current) => ({ ...current, [engine]: false }));
    }
  }

  async function restartBackend() {
    if (restartingBackend) return;
    const confirmed = await confirmDialog(
      "运行中的任务会被中断。Bosun 状态栏程序不会重启，图标和菜单仍会保留。确定重启后端？",
      { danger: true },
    );
    if (!confirmed) return;

    setRestartingBackend(true);
    try {
      await api.restartBackend();
      toast("后端正在重新启动", "success");
    } catch (err) {
      toast(`重启失败：${readDetail(err)}`, "error");
    } finally {
      setRestartingBackend(false);
    }
  }

  return (
    <div className="mx-auto w-full max-w-5xl space-y-7 p-4 lg:p-6">
      <SettingsGroup label="执行引擎">
      {!showClaude && <UninstalledEngineCard engine="cc" />}
      {showClaude && (
        <Section title="Claude" hint="调用方式、模型与推理档位。" aside={<EngineVersionChip tool={engineTools?.cc} />}>
          <Field label="调用方式">
            <select
              className="w-28"
              value={settings.claude_invocation}
              onChange={(e) => void onChange({ claude_invocation: e.target.value as AppSettings["claude_invocation"] })}
            >
              <option value="auto">自动</option>
              <option value="sdk">SDK</option>
              <option value="cli">CLI</option>
            </select>
          </Field>
          <Field label="模型">
            <div className="flex items-center gap-2">
              <ModelCombobox
                id="claude-model"
                value={settings.claude_model}
                options={settings.claude_model_options}
                onChange={(value) => onSettingsPatch({ claude_model: value })}
                onCommit={(value) => void onChange({ claude_model: value })}
              />
              <button
                type="button"
                aria-label="刷新 Claude 模型列表"
                disabled={refreshingModels.cc}
                onClick={() => void refreshModels("cc")}
                className="shrink-0 rounded-md border border-dh-bsoft bg-dh-surface px-3 py-1 text-sm text-dh-tsoft transition hover:border-teal-400 hover:text-dh-text disabled:cursor-not-allowed disabled:opacity-50"
              >
                {refreshingModels.cc ? "刷新中…" : "刷新"}
              </button>
            </div>
          </Field>
          <Field label="推理档位">
            <select
              className="w-28"
              value={settings.claude_effort}
              onChange={(e) => void onChange({ claude_effort: e.target.value })}
            >
              {settings.claude_effort_options.map((opt) => (
                <option key={opt.value || "default"} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>
          </Field>
        </Section>
      )}

      {!showCodex && <UninstalledEngineCard engine="codex" />}
      {showCodex && (
        <Section title="Codex" hint="模型与推理档位。" aside={<EngineVersionChip tool={engineTools?.codex} />}>
          <Field label="模型">
            <div className="flex items-center gap-2">
              <ModelCombobox
                id="codex-model"
                value={settings.codex_model}
                options={settings.codex_model_options}
                onChange={(value) => onSettingsPatch({ codex_model: value })}
                onCommit={(value) => void onChange({ codex_model: value })}
              />
              <button
                type="button"
                aria-label="刷新 Codex 模型列表"
                disabled={refreshingModels.codex}
                onClick={() => void refreshModels("codex")}
                className="shrink-0 rounded-md border border-dh-bsoft bg-dh-surface px-3 py-1 text-sm text-dh-tsoft transition hover:border-teal-400 hover:text-dh-text disabled:cursor-not-allowed disabled:opacity-50"
              >
                {refreshingModels.codex ? "刷新中…" : "刷新"}
              </button>
            </div>
          </Field>
          <Field
            label="推理档位"
            hint={settings.codex_effort === "ultra" ? "Ultra 需账号与所选模型支持，会显著增加用量" : undefined}
          >
            <select
              className="w-28"
              value={settings.codex_effort}
              onChange={(e) => void onChange({ codex_effort: e.target.value })}
            >
              {settings.codex_effort_options.map((opt) => (
                <option key={opt.value || "default"} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>
          </Field>
        </Section>
      )}

      {!showOmp && <UninstalledEngineCard engine="omp" />}
      {showOmp && (
        <Section title="Oh My Pi" hint="模型与思考档位。omp 的模型名跨 provider 模糊匹配，可直接填写。" aside={<EngineVersionChip tool={engineTools?.omp} />}>
          <Field label="模型">
            <ModelCombobox
              id="omp-model"
              value={settings.omp_model}
              options={settings.omp_model_options}
              onChange={(value) => onSettingsPatch({ omp_model: value })}
              onCommit={(value) => void onChange({ omp_model: value })}
            />
          </Field>
          <Field label="思考档位">
            <select
              className="w-28"
              value={settings.omp_thinking}
              onChange={(e) => void onChange({ omp_thinking: e.target.value })}
            >
              {settings.omp_thinking_options.map((opt) => (
                <option key={opt.value || "default"} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>
          </Field>
        </Section>
      )}

      {!showKimi && <UninstalledEngineCard engine="kimi" />}
      {showKimi && (
        <Section title="Kimi Code" hint="模型别名来自 ~/.kimi-code/config.toml；留空用 default_model。" aside={<EngineVersionChip tool={engineTools?.kimi} />}>
          <Field label="模型">
            <div className="flex items-center gap-2">
              <ModelCombobox
                id="kimi-model"
                value={settings.kimi_model}
                options={settings.kimi_model_options}
                onChange={(value) => onSettingsPatch({ kimi_model: value })}
                onCommit={(value) => void onChange({ kimi_model: value })}
              />
              <button
                type="button"
                aria-label="刷新 Kimi 模型列表"
                disabled={refreshingModels.kimi}
                onClick={() => void refreshModels("kimi")}
                className="shrink-0 rounded-md border border-dh-bsoft bg-dh-surface px-3 py-1 text-sm text-dh-tsoft transition hover:border-teal-400 hover:text-dh-text disabled:cursor-not-allowed disabled:opacity-50"
              >
                {refreshingModels.kimi ? "刷新中…" : "刷新"}
              </button>
            </div>
          </Field>
        </Section>
      )}

      </SettingsGroup>

      <SettingsGroup label="运行与系统">
        <Section title="运行" hint="控制同时执行的任务数量。">
          <Field label="并发上限">
            <input
              type="number"
              min={1}
              className="w-20 rounded-md border border-dh-bsoft bg-dh-surface px-2.5 py-1 text-sm text-dh-text"
              value={settings.max_concurrent}
              onChange={(e) => void onChange({ max_concurrent: Number(e.target.value) })}
            />
          </Field>
        </Section>
        <Section title="系统" hint="管理由 Bosun.app 托管的后端服务。">
          <Field label="后端服务" hint="只重启后端；状态栏图标和菜单不受影响。">
            <button
              type="button"
              disabled={restartingBackend}
              onClick={() => void restartBackend()}
              className="rounded-lg border border-rose-500/40 px-3 py-1.5 text-sm text-rose-500 hover:bg-rose-500/20 disabled:cursor-not-allowed disabled:opacity-50"
            >重启后端</button>
          </Field>
        </Section>
        <StorageOverview />
      </SettingsGroup>

      <SettingsGroup label="维护与安全">
        <BosunVersion />
        <AccessControl auth={auth} onAuthChanged={onAuthChanged} />
      </SettingsGroup>
    </div>
  );
}
