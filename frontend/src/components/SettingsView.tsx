import { useEffect, useRef, useState, type FormEvent } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api, type AppSettings, type AuthStatus, type SelfUpdateInfo, type SelfUpdateResult } from "../api";
import { setToken } from "../auth";
import { useEngineVisible } from "../installedEngines";
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
        className="w-full rounded-md border border-slate-200 bg-white py-1 pl-2 pr-7 text-sm text-slate-800 outline-none transition focus:border-teal-400 focus:ring-1 focus:ring-teal-400/30"
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
        className="absolute inset-y-0 right-0 grid w-7 place-items-center rounded-r-md text-[10px] text-slate-400 hover:bg-slate-50 hover:text-slate-600"
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
          className="absolute left-0 top-[calc(100%+0.3rem)] z-[80] max-h-56 min-w-full overflow-auto rounded-lg border border-slate-200 bg-white p-1 shadow-xl shadow-slate-900/15"
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
                    ? "bg-teal-50 font-medium text-teal-700"
                    : active
                      ? "bg-slate-100 text-slate-800"
                      : "text-slate-700 hover:bg-slate-50"
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

function Section({ title, hint, children }: { title: string; hint?: string; children: React.ReactNode }) {
  return (
    <section className="card p-4 lg:p-5">
      <h2 className="text-sm font-semibold text-slate-900">{title}</h2>
      {hint && <p className="mt-1 text-xs text-slate-400">{hint}</p>}
      <div className="mt-4 space-y-4">{children}</div>
    </section>
  );
}

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1.5">
      <div className="min-w-0">
        <div className="text-sm text-slate-700">{label}</div>
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
          className="rounded-lg border border-slate-200 px-3 py-1.5 text-sm text-slate-600 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
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
              className="text-xs text-teal-600 hover:underline"
            >更新说明</a>
            <button
              type="button"
              disabled={updating || !info.can_update}
              onClick={() => void update()}
              className="rounded-lg border border-teal-300 px-3 py-1.5 text-sm text-teal-600 hover:bg-teal-50 disabled:cursor-not-allowed disabled:opacity-50"
            >{updating ? "更新中…" : "立即更新"}</button>
          </div>
        </Field>
      )}

      {info?.update_available && info.release_notes && (
        <div className="chat-md max-h-40 overflow-auto rounded-lg border border-slate-200 bg-slate-50 px-3 py-2">
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            components={{ a: (p) => <a {...p} target="_blank" rel="noreferrer" /> }}
          >
            {info.release_notes}
          </ReactMarkdown>
        </div>
      )}

      {info && info.blockers.length > 0 && (
        <div className="rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-700">
          无法在线更新：{info.blockers.join("；")}
        </div>
      )}

      {result && (
        <div className="space-y-1 rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-[11px] text-slate-600">
          {result.steps.map((step, index) => (
            <div key={`${step.name}-${index}`} className="flex gap-2">
              <span className={step.skipped ? "text-slate-400" : step.ok ? "text-teal-600" : "text-rose-500"}>
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
        <div className="rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-700">
          ⚠️ 当前未启用登录，任何能访问本服务的人都能操作终端与任务。若本机以外可访问，请立刻设置口令。
        </div>
      )}
      {envManaged && (
        <div className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-500">
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
                className="w-52 rounded-md border border-slate-200 bg-white px-2.5 py-1 text-sm text-slate-800"
                value={currentPassword}
                onChange={(e) => setCurrentPassword(e.target.value)}
              />
            </Field>
          )}
          <Field label={auth.enabled ? "新口令" : "设置口令"} hint={`至少 ${auth.min_password_length} 位`}>
            <input
              type="password"
              autoComplete="new-password"
              className="w-52 rounded-md border border-slate-200 bg-white px-2.5 py-1 text-sm text-slate-800"
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
            />
          </Field>
          <Field label="确认口令">
            <input
              type="password"
              autoComplete="new-password"
              className="w-52 rounded-md border border-slate-200 bg-white px-2.5 py-1 text-sm text-slate-800"
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
            />
          </Field>
          {error && <div className="text-xs text-rose-500">{error}</div>}
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="submit"
              disabled={saving || !newPassword}
              className="rounded-lg bg-black px-3 py-1.5 text-sm font-medium text-white ring-1 ring-slate-700 disabled:opacity-50"
            >
              {saving ? "保存中…" : auth.enabled ? "修改口令" : "启用登录"}
            </button>
            {auth.enabled && (
              <button
                type="button"
                onClick={disableLogin}
                className="rounded-lg border border-rose-300 px-3 py-1.5 text-sm text-rose-500 hover:bg-rose-50"
              >
                关闭登录
              </button>
            )}
          </div>
        </form>
      )}

      {auth.enabled && (
        <div className="border-t border-slate-100 pt-3">
          <button
            type="button"
            onClick={logout}
            className="rounded-lg border border-slate-200 px-3 py-1.5 text-sm text-slate-600 hover:bg-slate-50"
          >
            退出登录
          </button>
        </div>
      )}
    </Section>
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
  });
  const [restartingBackend, setRestartingBackend] = useState(false);
  // 没装的引擎不显示它的模型/档位设置
  const showClaude = useEngineVisible("cc");
  const showCodex = useEngineVisible("codex");
  const showOmp = useEngineVisible("omp");
  // omp 没有可消费的模型目录接口，设置页直接填模型 ID，这里只服务 cc/codex。
  async function refreshModels(engine: "cc" | "codex") {
    if (refreshingModels[engine]) return;

    const engineLabel = engine === "cc" ? "Claude" : "Codex";
    setRefreshingModels((current) => ({ ...current, [engine]: true }));
    try {
      const result = await api.refreshModelOptions(engine);
      if (engine === "cc") {
        onSettingsPatch({ claude_model_options: result.model_options });
      } else {
        onSettingsPatch({ codex_model_options: result.model_options });
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
    <div className="mx-auto w-full max-w-3xl space-y-4 p-4 lg:p-6">
      <Section title="运行" hint="控制同时执行的任务数量。">
        <Field label="并发上限">
          <input
            type="number"
            min={1}
            className="w-20 rounded-md border border-slate-200 bg-white px-2.5 py-1 text-sm text-slate-800"
            value={settings.max_concurrent}
            onChange={(e) => void onChange({ max_concurrent: Number(e.target.value) })}
          />
        </Field>
      </Section>

      {showClaude && (
        <Section title="Claude" hint="调用方式、模型与推理档位。">
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
                className="shrink-0 rounded-md border border-slate-200 bg-white px-3 py-1 text-sm text-slate-700 transition hover:border-teal-400 hover:text-slate-900 disabled:cursor-not-allowed disabled:opacity-50"
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

      {showCodex && (
        <Section title="Codex" hint="模型与推理档位。">
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
                className="shrink-0 rounded-md border border-slate-200 bg-white px-3 py-1 text-sm text-slate-700 transition hover:border-teal-400 hover:text-slate-900 disabled:cursor-not-allowed disabled:opacity-50"
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

      {showOmp && (
        <Section title="Oh My Pi" hint="模型与思考档位。omp 的模型名跨 provider 模糊匹配，可直接填写。">
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

      <Section title="系统" hint="管理由 Bosun.app 托管的后端服务。">
        <Field label="后端服务" hint="只重启后端；状态栏图标和菜单不受影响。">
          <button
            type="button"
            disabled={restartingBackend}
            onClick={() => void restartBackend()}
            className="rounded-lg border border-rose-300 px-3 py-1.5 text-sm text-rose-500 hover:bg-rose-50 disabled:cursor-not-allowed disabled:opacity-50"
          >重启后端</button>
        </Field>
      </Section>

      <BosunVersion />

      <AccessControl auth={auth} onAuthChanged={onAuthChanged} />
    </div>
  );
}
