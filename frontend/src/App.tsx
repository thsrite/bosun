import { useCallback, useEffect, useRef, useState, type TouchEvent } from "react";
import { api, type AppSettings, type AuthStatus } from "./api";
import { WS_UNAUTHORIZED, getToken, onUnauthorized, setToken, wsProtocols } from "./auth";
import { AddProjectDialog } from "./components/AddProjectDialog";
import { ActiveTasksView } from "./components/ActiveTasksView";
import { ClaudeManagerView } from "./components/ClaudeManagerView";
import { HomeView } from "./components/HomeView";
import { ProjectView } from "./components/ProjectView";
import { LoginView } from "./components/LoginView";
import { ProposalsDialog } from "./components/ProposalsDialog";
import { QuotaBadge } from "./components/QuotaBadge";
import { SettingsView } from "./components/SettingsView";
import { InstalledEnginesContext, type InstalledEngines } from "./installedEngines";
import { OverlayHost, confirmDialog, toast } from "./overlay";
import { guardQuota } from "./quota";
import type { Engine, Project } from "./types";
import { useSingleFlight } from "./useSingleFlight";

type Tab = "projects" | "tasks" | "claude" | "stats" | "delivery" | "settings";
const NAV: { key: Tab; label: string }[] = [
  { key: "projects", label: "项目" },
  { key: "tasks", label: "运行任务" },
  { key: "claude", label: "Claude 管理" },
  { key: "stats", label: "统计图表" },
  { key: "delivery", label: "交付监控" },
  { key: "settings", label: "设置" },
];
const DEFAULT_AUTH: AuthStatus = {
  enabled: false,
  source: "none",
  authenticated: true,
  min_password_length: 6,
};
const DEFAULT_SETTINGS: AppSettings = {
  max_concurrent: 3,
  quota_enabled: true,
  claude_invocation: "auto",
  claude_model: "",
  claude_model_options: [{ value: "", label: "默认" }],
  claude_effort: "",
  claude_effort_options: [{ value: "", label: "默认" }],
  codex_model: "",
  codex_model_options: [{ value: "", label: "默认" }],
  codex_effort: "",
  codex_effort_options: [{ value: "", label: "默认" }],
  omp_model: "",
  omp_model_options: [{ value: "", label: "默认" }],
  omp_thinking: "",
  omp_thinking_options: [{ value: "", label: "默认" }],
  kimi_model: "",
  kimi_model_options: [{ value: "", label: "默认" }],
  browser: { available: false, missing: ["正在检测 Browser 运行条件"], model: "gpt-5.6" },
};
const PULL_REFRESH_TRIGGER_PX = 72;
const PULL_REFRESH_MAX_PX = 96;

type PullGesture = {
  startX: number;
  startY: number;
  locked: boolean;
  distance: number;
};


export default function App() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [settings, setSettings] = useState<AppSettings>(DEFAULT_SETTINGS);
  const [auth, setAuth] = useState<AuthStatus>(DEFAULT_AUTH);
  const [authReady, setAuthReady] = useState(false);
  const [view, setView] = useState<"home" | number>("home");
  const [tab, setTab] = useState<Tab>("projects");
  const [reloadKey, setReloadKey] = useState(0);
  const [showAdd, setShowAdd] = useState(false);
  const [showProposals, setShowProposals] = useState(false);
  const [hasPendingUpdates, setHasPendingUpdates] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [installedEngines, setInstalledEngines] = useState<InstalledEngines>(null);
  // null = 还没探测出结果；未装 claude CLI 时不展示 Claude 管理入口
  const claudeInstalled = installedEngines ? installedEngines.cc === true : null;
  const [pullDistance, setPullDistance] = useState(0);
  const { busy: startingAll, run: runStartAll } = useSingleFlight();
  const mainRef = useRef<HTMLElement | null>(null);
  const pullRef = useRef<PullGesture | null>(null);
  const refreshPromiseRef = useRef<Promise<void> | null>(null);

  const load = useCallback(async () => {
    setProjects(await api.projects());
  }, []);

  const loadProjectsAndClearPending = useCallback(async () => {
    await load();
    setHasPendingUpdates(false);
  }, [load]);

  // 从 URL hash 恢复视图(刷新后不回首页)
  const applyHash = useCallback(() => {
    const h = location.hash.replace(/^#\/?/, "");
    if (h.startsWith("project/")) {
      const id = Number(h.slice(8));
      if (id) return setView(id);
    }
    if (
      h === "stats" ||
      h === "delivery" ||
      h === "projects" ||
      h === "tasks" ||
      h === "claude" ||
      h === "settings"
    ) {
      setTab(h);
      setView("home");
    }
  }, []);

  const refreshAll = useCallback(() => {
    if (refreshPromiseRef.current) return refreshPromiseRef.current;
    const promise = (async () => {
      setRefreshing(true);
      try {
        await load();
        setReloadKey((k) => k + 1);
        setHasPendingUpdates(false);
      } catch (err) {
        toast(`刷新失败：${err instanceof Error ? err.message : String(err)}`, "error");
      } finally {
        setRefreshing(false);
        setPullDistance(0);
        pullRef.current = null;
        refreshPromiseRef.current = null;
      }
    })();
    refreshPromiseRef.current = promise;
    return promise;
  }, [load]);

  const refreshAuth = useCallback(async () => {
    try {
      setAuth({ ...DEFAULT_AUTH, ...(await api.auth.status()) });
    } catch {
      // 后端暂时不可达：按未启用登录处理，业务请求自己会报错
      setAuth(DEFAULT_AUTH);
    } finally {
      setAuthReady(true);
    }
  }, []);

  // token 失效（API 401 / 终端 WS 4401）时重新拉鉴权状态，把用户弹回登录页
  useEffect(() => {
    void refreshAuth();
    return onUnauthorized(() => {
      void refreshAuth();
    });
  }, [refreshAuth]);

  const signedIn = !auth.enabled || auth.authenticated;

  useEffect(() => {
    if (!signedIn) return;
    load();
    applyHash();
    window.addEventListener("hashchange", applyHash);
    api.getSettings().then((s) => {
      setSettings({ ...DEFAULT_SETTINGS, ...s }); // 兜底: 后端漏返回的字段用默认, 防 .map 崩
    });
    // 事件 WS：只提示有新事件，不主动刷新列表；刷新由下拉/按钮/业务操作触发。
    const proto = location.protocol === "https:" ? "wss" : "ws";
    let ws: WebSocket | null = null;
    let retry: number | null = null;
    let closed = false;
    const connect = () => {
      ws = new WebSocket(`${proto}://${location.host}/ws/events`, wsProtocols());
      ws.onmessage = (e) => {
        setHasPendingUpdates(true);
        try {
          const msg = JSON.parse(e.data);
          // 状态变化直接刷新任务视图；任务列表是运行监控面板，不能要求用户手动刷新
          // 才知道某一轮已经完成或正在等人介入。
          if (msg.type === "task.status" || msg.type === "task.tokens") {
            setReloadKey((k) => k + 1);
          }
        } catch {
          /* ignore */
        }
      };
      ws.onclose = (ev) => {
        // 会话失效：清 token 弹回登录页，别再无谓重连
        if (ev.code === WS_UNAUTHORIZED) {
          setToken(null);
          return;
        }
        if (!closed) retry = window.setTimeout(connect, 2000);
      };
    };
    connect();
    return () => {
      closed = true;
      if (retry) clearTimeout(retry);
      window.removeEventListener("hashchange", applyHash);
      ws?.close();
    };
  }, [signedIn, load, applyHash]);

  // 未安装的引擎不展示额度/设置/任务分配等相关信息，探测一次全局共享
  useEffect(() => {
    if (!signedIn) return;
    api.engineTools
      .installed()
      .then(setInstalledEngines)
      .catch(() => setInstalledEngines(null));
  }, [signedIn, reloadKey]);

  // 已经停在 Claude 管理却探测到没装：退回项目页，别留个空页面
  useEffect(() => {
    if (claudeInstalled === false && tab === "claude") {
      setTab("projects");
      location.hash = "#projects";
    }
  }, [claudeInstalled, tab]);

  const resetPull = useCallback(() => {
    pullRef.current = null;
    setPullDistance(0);
  }, []);

  const onMainTouchStart = useCallback((e: TouchEvent<HTMLElement>) => {
    if (refreshing || e.touches.length !== 1) return;
    const target = e.target as HTMLElement;
    if (
      target.closest(
        "button,a,input,textarea,select,[data-no-pull-refresh],.dh-terminal-selectable",
      )
    ) {
      return;
    }
    const main = mainRef.current;
    if (!main || main.scrollTop > 0) return;
    const touch = e.touches[0];
    pullRef.current = {
      startX: touch.clientX,
      startY: touch.clientY,
      locked: false,
      distance: 0,
    };
  }, [refreshing]);

  const onMainTouchMove = useCallback((e: TouchEvent<HTMLElement>) => {
    const gesture = pullRef.current;
    if (!gesture || e.touches.length !== 1) return;
    const main = mainRef.current;
    if (!main || main.scrollTop > 0) {
      resetPull();
      return;
    }
    const touch = e.touches[0];
    const dx = touch.clientX - gesture.startX;
    const dy = touch.clientY - gesture.startY;
    if (dy <= 0) {
      gesture.distance = 0;
      setPullDistance(0);
      return;
    }
    if (!gesture.locked) {
      if (Math.abs(dx) > 10 && Math.abs(dx) > Math.abs(dy) * 1.2) {
        resetPull();
        return;
      }
      if (dy < 10) return;
      gesture.locked = true;
    }
    e.preventDefault();
    const distance = Math.min(PULL_REFRESH_MAX_PX, Math.round(dy * 0.45));
    gesture.distance = distance;
    setPullDistance(distance);
  }, [resetPull]);

  const onMainTouchEnd = useCallback(() => {
    const distance = pullRef.current?.distance ?? 0;
    pullRef.current = null;
    if (distance >= PULL_REFRESH_TRIGGER_PX) {
      void refreshAll();
    } else {
      setPullDistance(0);
    }
  }, [refreshAll]);

  const running = projects.reduce((a, p) => a + p.running, 0);
  const maxConcurrent = settings.max_concurrent;
  const activeTotal = projects.reduce((a, p) => a + p.running + (p.by_status?.queued ?? 0), 0);
  const totalDrafts = projects.reduce((a, p) => a + (p.by_status?.draft ?? 0), 0);
  const totalWaiting = projects.reduce((a, p) => a + (p.by_status?.waiting_input ?? 0), 0);
  const current = typeof view === "number" ? projects.find((p) => p.id === view) : null;
  const inProject = view !== "home" && !!current;
  const showPullIndicator = refreshing || pullDistance > 0;
  const pullReady = pullDistance >= PULL_REFRESH_TRIGGER_PX;
  const pullLabel = refreshing ? "刷新中…" : pullReady ? "松开刷新" : "下拉刷新";

  async function changeSettings(patch: Partial<AppSettings>) {
    const next = { ...settings, ...patch, max_concurrent: Math.max(1, patch.max_concurrent ?? settings.max_concurrent) };
    const saved = await api.setSettings(next);
    setSettings({ ...DEFAULT_SETTINGS, ...saved });
  }

  const updateModelOptions = useCallback((
    engine: "cc" | "codex",
    options: AppSettings["claude_model_options"],
  ) => {
    setSettings((current) => (
      engine === "cc"
        ? { ...current, claude_model_options: options }
        : { ...current, codex_model_options: options }
    ));
  }, []);

  function goTab(t: Tab) {
    setTab(t);
    setView("home");
    location.hash = "#" + t;
  }
  function openProject(id: number) {
    setView(id);
    location.hash = "#project/" + id;
  }
  function backHome() {
    setView("home");
    location.hash = "#" + tab;
  }

  async function deleteProject(project: Project) {
    const active = project.running + (project.by_status?.queued ?? 0);
    const suffix = active > 0 ? `\n\n当前有 ${active} 个运行中/排队任务，删除会先终止并清理这些任务。` : "";
    const ok = await confirmDialog(
      `删除项目「${project.name}」？\n\n只会从 Bosun 移除项目和关联记录，不会删除磁盘目录：\n${project.path}${suffix}`,
      { danger: true },
    );
    if (!ok) return;
    try {
      await api.deleteProject(project.id);
      if (view === project.id) {
        setView("home");
        location.hash = "#" + tab;
      }
      setProjects((prev) => prev.filter((p) => p.id !== project.id));
      await loadProjectsAndClearPending();
      setReloadKey((k) => k + 1);
      toast(`已删除项目「${project.name}」`, "success");
    } catch (err) {
      const raw = err instanceof Error ? err.message : String(err);
      let detail = raw;
      try {
        detail = JSON.parse(raw).detail || raw;
      } catch {
        /* keep raw message */
      }
      toast(`删除失败：${detail}`, "error");
    }
  }

  // 鉴权状态未知时先不渲染，避免工作台闪一下又被登录页顶掉
  if (!authReady) return <div className="dh-app-shell h-full" />;
  if (!signedIn) return <LoginView onSignedIn={refreshAuth} />;

  return (
    <InstalledEnginesContext.Provider value={installedEngines}>
      <div className="dh-app-shell flex h-full min-w-0 flex-col">
        <header className="dh-app-header relative z-50 border-b border-dh-bsoft bg-dh-soft/95">
          <div className="flex flex-wrap items-center gap-2 px-3 py-2 sm:px-4 sm:py-2.5 lg:gap-3 lg:px-8">
            <button
              className="flex min-w-0 items-center gap-2 pr-2 text-left"
              onClick={() => goTab("projects")}
            >
              <span className="grid h-8 w-8 shrink-0 place-items-center overflow-hidden rounded-lg border border-dh-border bg-black">
                <img src="/icons/bosun.svg" alt="" className="h-full w-full" />
              </span>
              {/* 移动端顶栏寸土寸金：只留 icon，标题文字 sm 起才显示 */}
              <span className="hidden min-w-0 sm:block">
                <span className="block text-sm font-semibold leading-tight text-dh-text">Bosun 工作台</span>
              </span>
            </button>

            <nav className="dh-scrollbar-none flex min-w-0 flex-1 items-center gap-1 overflow-x-auto">
              {NAV.filter((n) => n.key !== "claude" || claudeInstalled === true).map((n) => {
                const active = !inProject && tab === n.key;
                return (
                  <button
                    key={n.key}
                    onClick={() => goTab(n.key)}
                    className={`flex shrink-0 items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-sm transition lg:px-3 ${
                      active
                        ? "bg-black font-medium text-white ring-1 ring-slate-700"
                        : "text-dh-tsoft hover:bg-dh-hover"
                    }`}
                  >
                    <span>{n.label}</span>
                    {n.key === "tasks" && activeTotal > 0 && (
                      <span className="rounded-full bg-emerald-500/15 px-1.5 text-[10px] font-semibold text-emerald-300 ring-1 ring-dh-border">
                        {activeTotal}
                      </span>
                    )}
                  </button>
                );
              })}
            </nav>

            <div className="flex w-full items-center gap-2 sm:w-auto lg:gap-3">
              {totalWaiting > 0 && (
                <button
                  className="flex animate-pulse items-center gap-1.5 rounded-lg border border-amber-500/40 bg-amber-500/10 px-2.5 py-1.5 text-xs font-medium text-amber-400 hover:bg-amber-500/20 md:py-1"
                  onClick={() => goTab("tasks")}
                  title="有任务在等待你输入/介入"
                >
                  <span className="h-2 w-2 rounded-full bg-amber-500" />
                  {totalWaiting}
                  <span className="hidden sm:inline"> 待输入</span>
                </button>
              )}
              <QuotaBadge onModelOptionsUpdated={updateModelOptions} />
              {totalDrafts > 0 && (
                <button
                  className="rounded-lg border border-slate-400/30 bg-slate-400/10 px-2.5 py-1.5 text-sm font-medium text-dh-tsoft hover:bg-slate-400/15 disabled:opacity-50 lg:px-3"
                  disabled={startingAll}
                  onClick={() =>
                    runStartAll(async () => {
                      if (!(await guardQuota())) return;
                      if (await confirmDialog(`把全部 ${totalDrafts} 个待办任务排入执行？`)) {
                        await api.startAll();
                        await refreshAll();
                      }
                    })
                  }
                >
                  ▶ <span className="hidden sm:inline">全部执行 </span>({totalDrafts})
                </button>
              )}
            </div>
          </div>
        </header>

        <main
          ref={mainRef}
          className="relative min-h-0 flex-1 overflow-auto"
          onTouchStart={onMainTouchStart}
          onTouchMove={onMainTouchMove}
          onTouchEnd={onMainTouchEnd}
          onTouchCancel={resetPull}
        >
          {showPullIndicator && (
            <div className="pointer-events-none sticky top-0 z-30 flex h-0 justify-center">
              <div
                className="mt-2 rounded-full border border-dh-border bg-dh-soft/95 px-3 py-1.5 text-xs font-medium text-slate-200 shadow-lg"
                style={{
                  transform: `translateY(${Math.max(0, (refreshing ? 54 : pullDistance) - 48)}px)`,
                }}
              >
                {pullLabel}
              </div>
            </div>
          )}
          {inProject ? (
            <ProjectView
              project={current!}
              reloadKey={reloadKey}
              onBack={backHome}
              onDataChanged={loadProjectsAndClearPending}
              onDeleteProject={deleteProject}
            />
          ) : tab === "tasks" ? (
            <ActiveTasksView
              projects={projects}
              reloadKey={reloadKey}
              onDataChanged={loadProjectsAndClearPending}
              runningTotal={running}
              maxConcurrent={maxConcurrent}
              hasPendingUpdates={hasPendingUpdates}
              refreshing={refreshing}
            />
          ) : tab === "claude" && claudeInstalled !== false ? (
            <ClaudeManagerView />
          ) : tab === "settings" ? (
            <SettingsView
              settings={settings}
              onChange={changeSettings}
              onSettingsPatch={(patch) => setSettings((current) => ({ ...current, ...patch }))}
              auth={auth}
              onAuthChanged={refreshAuth}
            />
          ) : (
            <HomeView
              projects={projects}
              tab={tab === "claude" ? "projects" : tab}
              onOpen={openProject}
              onAddProject={() => setShowAdd(true)}
              onProposals={() => setShowProposals(true)}
              onDeleteProject={deleteProject}
            />
          )}
        </main>

        {showAdd && <AddProjectDialog onClose={() => setShowAdd(false)} onDone={loadProjectsAndClearPending} />}
        {showProposals && <ProposalsDialog onClose={() => setShowProposals(false)} />}
        <OverlayHost />
      </div>
    </InstalledEnginesContext.Provider>
  );
}
