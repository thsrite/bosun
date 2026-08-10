import { authHeaders, setToken } from "./auth";
import type { Engine, Finding, IssueSource, LocalSession, Project, Task } from "./types";

export type AppSettings = {
  max_concurrent: number;
  claude_invocation: "auto" | "sdk" | "cli";
  claude_model: string;
  claude_model_options: { value: string; label: string }[];
  claude_effort: string;
  claude_effort_options: { value: string; label: string }[];
  codex_model: string;
  codex_model_options: { value: string; label: string }[];
  codex_effort: string;
  codex_effort_options: { value: string; label: string }[];
  omp_model: string;
  omp_model_options: { value: string; label: string }[];
  omp_thinking: string;
  omp_thinking_options: { value: string; label: string }[];
  kimi_model: string;
  kimi_model_options: { value: string; label: string }[];
};

export type StorageInfo = {
  data_dir: string;
  db_path: string;
  db_size: number;
  log_dir: string;
  log_size: number;
  log_count: number;
  other_size: number;
  total_size: number;
  total_count: number;
  archived_count: number;
  archived_size: number;
};

export type StorageCompressResult = StorageInfo & {
  compressed_count: number;
  saved_size: number;
};

export type ClaudeResourceCategory =
  | "memory"
  | "settings"
  | "skill"
  | "rule"
  | "command"
  | "agent"
  | "hook"
  | "file";

export type ClaudeResource = {
  path: string;
  name: string;
  label: string;
  category: ClaudeResourceCategory;
  category_label: string;
  disk_path: string;
  enabled_path: string;
  disabled_path: string;
  enabled: boolean;
  toggleable: boolean;
  exists: boolean;
  size: number;
  updated_at: number | null;
  deletable: boolean;
};

export type ClaudeResourceContent = ClaudeResource & {
  content: string;
};

export type EngineToolInfo = {
  engine: string;
  label: string;
  configured_binary?: string | null;
  binary?: string | null;
  installed?: boolean;
  version?: string | null;
  cli_version?: string | null;
  version_raw?: string | null;
  package_manager?: string | null;
  package_name?: string | null;
  package_version?: string | null;
  latest_package_name?: string | null;
  update_command?: string | null;
  update_supported?: boolean;
  can_check_update?: boolean;
  can_update?: boolean;
  update_error?: string | null;
  latest_version?: string | null;
  update_available?: boolean | null;
  checked_at?: number | null;
  error?: string | null;
};

export type EngineToolUpdateResult = {
  ok: boolean;
  engine: string;
  package_name?: string | null;
  update_command?: string | null;
  exit_code?: number | null;
  before_version?: string | null;
  after_version?: string | null;
  before?: EngineToolInfo;
  after?: EngineToolInfo;
  stdout?: string;
  stderr?: string;
  error?: string | null;
  model_options?: { value: string; label: string }[];
  model_options_error?: string | null;
};

export type SelfUpdateInfo = {
  repo: string;
  releases_url: string;
  current_version: string;
  branch?: string | null;
  detached?: boolean;
  dirty?: boolean;
  head?: string | null;
  is_git?: boolean;
  blockers: string[];
  can_update: boolean;
  latest_version?: string | null;
  latest_tag?: string | null;
  update_available?: boolean | null;
  release_notes?: string | null;
  release_url?: string | null;
  published_at?: string | null;
  check_error?: string | null;
  checked_at?: number | null;
};

export type SelfUpdateStep = {
  name: string;
  command?: string | null;
  exit_code?: number | null;
  ok: boolean;
  skipped?: boolean;
  output?: string;
};

export type SelfUpdateResult = {
  ok: boolean;
  changed: boolean;
  steps: SelfUpdateStep[];
  error?: string | null;
  from_version?: string | null;
  to_version?: string | null;
  tag?: string | null;
  message?: string | null;
  restart?: "scheduled" | "manual" | "none";
  restart_hint?: string | null;
};

export type ReflectionSettings = {
  auto_enabled: boolean;
  interval_minutes: number;
  min_pending_gap: number;
  last_run_at: number | null;
  last_skip_reason: string;
  last_new_count?: number;
  running?: boolean;
};

export type ProposalTaskSummary = {
  id: number;
  project_id: number;
  title: string | null;
  status: string;
  engine: Engine;
};

export type ProposalAction = {
  type: string;
  [key: string]: unknown;
};

export type ProposalItem = {
  id: number;
  title: string;
  rationale: string | null;
  action: ProposalAction | null;
  status: string;
  task_id: number | null;
  task: ProposalTaskSummary | null;
  created_at: number;
  applied_at: number | null;
};

export type ProposalApplyResult = {
  applied: boolean;
  note: string;
  task_id: number | null;
};

export type HarnessCluster = {
  id: number;
  engine: Engine;
  cause: string;
  causal: "harness_gap" | "model_limit" | "env_issue" | "user_input";
  mechanism: string;
  episode_ids: string[];
  support: number;
  created_at: number;
};

export type HarnessMineStatus = {
  running: boolean;
  last_run_at: number | null;
  last_clusters: number | null;
  last_proposals: number | null;
  last_error: string | null;
};

export type HarnessVersionInfo = {
  engine: Engine;
  version: number;
  id: number;
  versions_total: number;
  can_rollback: 0 | 1;
};

export type HostMetrics = {
  generated_at: number;
  cpu_temp_c: number | null;
  cpu_load_pct: number | null;
  memory_load_pct: number | null;
  disk_load_pct: number | null;
};

export type AuthStatus = {
  enabled: boolean;
  source: "env" | "db" | "none";
  authenticated: boolean;
  min_password_length: number;
};

// 模块内遮蔽全局 fetch：所有 API 调用自动带上会话 token，401 时清 token 弹回登录页。
// 全项目的 fetch 调用都收在本文件，所以这一处封装即可覆盖，无需改调用点。
const rawFetch = globalThis.fetch.bind(globalThis);

function fetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  return rawFetch(input, { ...init, headers: authHeaders(init?.headers) }).then((res) => {
    if (res.status === 401) setToken(null);
    return res;
  });
}

async function j<T>(res: Response): Promise<T> {
  if (!res.ok) throw new Error((await res.text()) || res.statusText);
  return res.json();
}

const inFlightWrites = new Map<string, Promise<unknown>>();

function write<T>(method: string, url: string, body?: unknown): Promise<T> {
  const bodyText = body === undefined ? undefined : JSON.stringify(body);
  const key = `${method}:${url}:${bodyText ?? ""}`;
  const existing = inFlightWrites.get(key);
  if (existing) return existing as Promise<T>;

  const init: RequestInit = { method };
  if (body !== undefined) {
    init.headers = { "content-type": "application/json" };
    init.body = bodyText;
  }
  const req = fetch(url, init).then((r) => j<T>(r)).finally(() => inFlightWrites.delete(key));
  inFlightWrites.set(key, req);
  return req;
}

export const api = {
  projects: () => fetch("/api/projects").then((r) => j<Project[]>(r)),
  addProject: (path: string, name?: string) =>
    write<{ id: number }>("POST", "/api/projects", { path, name }),
  scan: (root: string) =>
    write<{ added: any[]; count: number }>("POST", "/api/projects/scan", { root }),
  deleteProject: (id: number) =>
    write<{ ok: boolean; deleted_tasks: number }>("DELETE", `/api/projects/${id}`),
  browse: (path?: string) =>
    fetch(`/api/projects/browse${path ? `?path=${encodeURIComponent(path)}` : ""}`).then((r) =>
      j<{
        path: string;
        parent: string | null;
        is_git: boolean;
        entries: { name: string; path: string; is_git: boolean }[];
      }>(r),
    ),

  tasks: (projectId?: number) =>
    fetch(projectId == null ? "/api/tasks" : `/api/tasks?project_id=${projectId}`).then((r) => j<Task[]>(r)),
  getTask: (id: number) => fetch(`/api/tasks/${id}`).then((r) => j<Task>(r)),
  updateTask: (id: number, body: { title?: string; prompt?: string; engine?: Engine }) =>
    write<Task>("PUT", `/api/tasks/${id}`, body),
  getLog: (id: number, source: "auto" | "terminal" | "script" = "auto") =>
    fetch(`/api/tasks/${id}/log?source=${source}`).then((r) =>
      j<{ log: string; source?: "terminal" | "script" }>(r),
    ),
  getHistory: (id: number) =>
    fetch(`/api/tasks/${id}/history`).then((r) =>
      j<{ messages: import("./types").SessionHistoryMessage[]; truncated: boolean }>(r),
    ),
  uploadFile: (id: number, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return fetch(`/api/tasks/${id}/upload-file`, { method: "POST", body: form }).then((r) =>
      j<{ path: string }>(r),
    );
  },
  transcribeAudio: (id: number, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return fetch(`/api/tasks/${id}/transcribe-audio`, { method: "POST", body: form }).then((r) =>
      j<{ text: string }>(r),
    );
  },
  createTask: (body: {
    project_id: number;
    engine: string;
    prompt: string;
    priority: number;
    auto_approve: boolean;
    start?: boolean;
  }) =>
    write<{ id: number; engine: string; auto_reason: string | null }>("POST", "/api/tasks", body),
  startTask: (id: number) =>
    write("POST", `/api/tasks/${id}/start`),
  startAll: (projectId?: number) =>
    write("POST", "/api/tasks/start-all", { project_id: projectId ?? null }),
  completeTask: (id: number) =>
    write("POST", `/api/tasks/${id}/complete`),
  pauseTask: (id: number) =>
    write("POST", `/api/tasks/${id}/pause`),
  resumePausedTask: (id: number, prompt = "") =>
    write("POST", `/api/tasks/${id}/resume-paused`, { prompt }),
  restorePausedTask: (id: number) =>
    write("POST", `/api/tasks/${id}/restore-paused`),
  toDraft: (id: number) =>
    write("POST", `/api/tasks/${id}/to-draft`),
  rerunTask: (id: number) =>
    write<{ id: number }>("POST", `/api/tasks/${id}/rerun`),
  continueTask: (id: number, body: { prompt?: string; compact?: boolean; start?: boolean }) =>
    write<{ id: number }>("POST", `/api/tasks/${id}/continue`, body),
  handoffTask: (id: number, engine: Engine, start = true) =>
    write<{ id: number; engine: Engine; from_task_id: number }>(
      "POST", `/api/tasks/${id}/handoff`, { engine, start },
    ),
  exportSession: (id: number) =>
    fetch(`/api/tasks/${id}/export`).then((r) => j<any>(r)),
  importSession: (projectId: number, bundle: any) =>
    write<{ task_id: number }>("POST", "/api/sessions/import", { project_id: projectId, bundle }),
  discoverLocalSessions: (projectId: number) =>
    fetch(`/api/sessions/discover?project_id=${projectId}`).then((r) => j<{ sessions: LocalSession[] }>(r)),
  attachLocalSession: (projectId: number, engine: string, sessionUid: string) =>
    write<{ task_id: number; created: boolean; status: string }>("POST", "/api/sessions/attach", {
      project_id: projectId,
      engine,
      session_uid: sessionUid,
    }),
  reorder: (items: { id: number; priority: number }[]) =>
    write("POST", "/api/tasks/reorder", { items }),
  cancelTask: (id: number) =>
    write("POST", `/api/tasks/${id}/cancel`),
  getPermission: (id: number) =>
    fetch(`/api/tasks/${id}/permission`).then((r) => j<{ permission: { tool: string; input: string } | null }>(r)),
  respondPermission: (id: number, allow: boolean) =>
    write("POST", `/api/tasks/${id}/permission`, { allow }),
  deleteTask: (id: number) =>
    write("DELETE", `/api/tasks/${id}`),

  findings: (projectId: number) =>
    fetch(`/api/findings?project_id=${projectId}&status=active`).then((r) => j<Finding[]>(r)),
  unmuteFinding: (id: number) =>
    write("POST", `/api/findings/${id}/unmute`),
  findingsMeta: (projectId: number) =>
    fetch(`/api/findings/meta?project_id=${projectId}`).then((r) =>
      j<{ last_analyze_at: number | null; audit_skipped: boolean }>(r)
    ),
  analyze: (projectId: number) =>
    write<{ new_findings: number }>("POST", "/api/findings/analyze", { project_id: projectId }),
  dismissFinding: (id: number) =>
    write("POST", `/api/findings/${id}/dismiss`),
  findingToTask: (id: number, engine: string, autoApprove = false) =>
    write<{ task_id: number }>("POST", `/api/findings/${id}/to-task`, { engine, priority: 7, auto_approve: autoApprove }),

  sources: {
    list: (projectId: number) =>
      fetch(`/api/projects/${projectId}/sources`).then((r) => j<IssueSource[]>(r)),
    create: (projectId: number, body: any) =>
      write<{ id: number }>("POST", `/api/projects/${projectId}/sources`, body),
    update: (id: number, body: any) =>
      write("PUT", `/api/sources/${id}`, body),
    remove: (id: number) =>
      write("DELETE", `/api/sources/${id}`),
    pull: (id: number) =>
      write<{ new_findings: number }>("POST", `/api/sources/${id}/pull`),
    test: (projectId: number, body: any) =>
      write<{ count: number; sample: { title: string; detail: string }[] }>("POST", `/api/projects/${projectId}/sources/test`, body),
  },

  quota: (engine?: string) =>
    fetch(engine ? `/api/quota?engine=${encodeURIComponent(engine)}` : "/api/quota").then((r) => j<any>(r)),
  engineTools: {
    /** 各引擎是否已安装；轻量探测，不跑 --version。 */
    installed: () => fetch("/api/quota/engines").then((r) => j<Record<string, boolean>>(r)),
    list: () => fetch("/api/quota/tools").then((r) => j<Record<string, EngineToolInfo>>(r)),
    get: (engine: string) =>
      fetch(`/api/quota/tools/${encodeURIComponent(engine)}`).then((r) => j<EngineToolInfo>(r)),
    checkUpdate: (engine: string) =>
      write<EngineToolInfo>("POST", `/api/quota/tools/${encodeURIComponent(engine)}/check-update`),
    update: (engine: string) =>
      write<EngineToolUpdateResult>("POST", `/api/quota/tools/${encodeURIComponent(engine)}/update`),
  },

  selfUpdate: {
    status: () => fetch("/api/self-update").then((r) => j<SelfUpdateInfo>(r)),
    check: () => write<SelfUpdateInfo>("POST", "/api/self-update/check"),
    run: () => write<SelfUpdateResult>("POST", "/api/self-update/run"),
  },

  proposals: {
    reflect: () => write<{ started: boolean; status: "started" | "running" }>("POST", "/api/proposals/reflect"),
    list: () => fetch("/api/proposals?status=pending").then((r) => j<ProposalItem[]>(r)),
    getSettings: () => fetch("/api/proposals/settings").then((r) => j<ReflectionSettings>(r)),
    updateSettings: (body: { auto_enabled: boolean; interval_minutes: number; min_pending_gap: number }) =>
      write<ReflectionSettings>("PUT", "/api/proposals/settings", body),
    apply: (id: number) =>
      write<ProposalApplyResult>("POST", `/api/proposals/${id}/apply`),
    dismiss: (id: number, reason = "") => write("POST", `/api/proposals/${id}/dismiss`, { reason }),
    harness: {
      mine: () => write<{ started: boolean; status: "started" | "running" }>("POST", "/api/proposals/harness/mine"),
      status: () => fetch("/api/proposals/harness/status").then((r) => j<HarnessMineStatus>(r)),
      clusters: () => fetch("/api/proposals/harness/clusters").then((r) => j<HarnessCluster[]>(r)),
      versions: () => fetch("/api/proposals/harness/versions").then((r) => j<HarnessVersionInfo[]>(r)),
      rollback: (engine: string) =>
        write<{ engine: string; active_version: number }>("POST", `/api/proposals/harness/rollback/${encodeURIComponent(engine)}`),
    },
  },

  auth: {
    status: () => fetch("/api/auth/status").then((r) => j<AuthStatus>(r)),
    login: (password: string) =>
      write<{ token: string; enabled: boolean }>("POST", "/api/auth/login", { password }),
    logout: () => write<{ ok: boolean }>("POST", "/api/auth/logout"),
    setPassword: (newPassword: string, currentPassword: string) =>
      write<{ token: string; enabled: boolean }>("PUT", "/api/auth/password", {
        new_password: newPassword,
        current_password: currentPassword,
      }),
    disablePassword: () => write<{ ok: boolean }>("DELETE", "/api/auth/password"),
  },
  getSettings: () => fetch("/api/settings").then((r) => j<AppSettings>(r)),
  setSettings: (settings: AppSettings) =>
    write<AppSettings>("PUT", "/api/settings", settings),
  refreshModelOptions: (engine: "cc" | "codex" | "kimi") =>
    write<{
      engine: "cc" | "codex";
      model_options: Array<{ value: string; label: string }>;
    }>("POST", `/api/settings/models/${engine}/refresh`),
  restartBackend: () =>
    write<{ accepted: boolean }>("POST", "/api/settings/restart"),
  getStorageInfo: () =>
    fetch("/api/settings/storage").then((r) => j<StorageInfo>(r)),
  compressStorage: () =>
    write<StorageCompressResult>("POST", "/api/settings/storage/compress"),

  claude: {
    list: () =>
      fetch("/api/claude/resources").then((r) =>
        j<{ root: string; resources: ClaudeResource[]; categories: Record<string, string> }>(r),
      ),
    get: (path: string) =>
      fetch(`/api/claude/resource?path=${encodeURIComponent(path)}`).then((r) =>
        j<ClaudeResourceContent>(r),
      ),
    save: (path: string, content: string) =>
      write<ClaudeResourceContent>("PUT", "/api/claude/resource", { path, content }),
    create: (kind: string, name: string, content?: string) =>
      write<ClaudeResourceContent>("POST", "/api/claude/resource", { kind, name, content }),
    remove: (path: string) =>
      write<{ ok: boolean; deleted: boolean }>(
        "DELETE",
        `/api/claude/resource?path=${encodeURIComponent(path)}`,
      ),
    setEnabled: (path: string, enabled: boolean) =>
      write<ClaudeResourceContent>("PUT", "/api/claude/resource/enabled", { path, enabled }),
  },

  autopilot: {
    start: (body: {
      project_id: number;
      max_iterations: number;
      fix_engine: string;
      review_engine: string;
      token_budget?: number;
      scope?: string;
      scope_arg?: string | null;
      force?: boolean;
    }) =>
      write<any>("POST", "/api/autopilot/start", body),
    stop: (runId: number) =>
      write("POST", `/api/autopilot/${runId}/stop`),
    list: (projectId: number) =>
      fetch(`/api/autopilot?project_id=${projectId}`).then((r) => j<any[]>(r)),
    log: (runId: number) => fetch(`/api/autopilot/${runId}/log`).then((r) => j<{ log: string }>(r)),
    spans: (runId: number) => fetch(`/api/autopilot/${runId}/spans`).then((r) => j<any[]>(r)),
    policies: (projectId: number) =>
      fetch(`/api/autopilot/policies?project_id=${projectId}`).then((r) => j<any[]>(r)),
    createPolicy: (body: any) =>
      write<any>("POST", "/api/autopilot/policies", body),
    patchPolicy: (id: number, body: any) =>
      write<any>("PATCH", `/api/autopilot/policies/${id}`, body),
    deletePolicy: (id: number) =>
      write("DELETE", `/api/autopilot/policies/${id}`),
    runPolicyNow: (id: number) =>
      write<any>("POST", `/api/autopilot/policies/${id}/run-now`),
  },

  stats: {
    dashboard: (days = 30) => fetch(`/api/stats/dashboard?days=${days}`).then((r) => j<any>(r)),
    host: () => fetch("/api/stats/host").then((r) => j<HostMetrics>(r)),
    overview: () => fetch("/api/stats/overview").then((r) => j<any>(r)),
    engines: () => fetch("/api/stats/engines").then((r) => j<any>(r)),
    findings: () => fetch("/api/stats/findings").then((r) => j<any>(r)),
    timeline: () => fetch("/api/stats/timeline").then((r) => j<any>(r)),
    activity: () => fetch("/api/stats/activity").then((r) => j<any>(r)),
    tokens: () => fetch("/api/stats/tokens").then((r) => j<any>(r)),
    tokensTimeline: () => fetch("/api/stats/tokens-timeline").then((r) => j<any>(r)),
  },
};
