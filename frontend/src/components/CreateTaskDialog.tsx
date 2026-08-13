import { useEffect, useRef, useState, type ClipboardEvent as ReactClipboardEvent } from "react";
import { api } from "../api";
import { toast } from "../overlay";
import { guardQuota } from "../quota";
import type { Engine, OrchestrationTemplate, Project } from "../types";
import { useSingleFlight } from "../useSingleFlight";
import { useAvailableTaskEngines, useInstalledEngines } from "../installedEngines";
import { isCoarsePointer } from "../pointer";
import { AttachmentPicker } from "./AttachmentPicker";
import { Modal } from "./Modal";
import { AUTO_APPROVE_FLAG, TASK_ENGINE_ORDER, engineName } from "../engines";

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

type PendingAttachment = {
  id: string;
  file: File;
  preview?: string;
};

type RememberedTaskSettings = {
  projectId?: number;
  engine?: "auto" | Engine;
  priority?: number;
  autoApprove?: boolean;
  executionMode?: "single" | "orchestration";
  orchestrationId?: number;
};

const TASK_SETTINGS_KEY = "bosun.create-task-settings";

function rememberedTaskSettings(): RememberedTaskSettings {
  try {
    return JSON.parse(localStorage.getItem(TASK_SETTINGS_KEY) ?? "{}") as RememberedTaskSettings;
  } catch {
    return {};
  }
}

function rememberTaskSettings(settings: RememberedTaskSettings) {
  try {
    localStorage.setItem(TASK_SETTINGS_KEY, JSON.stringify(settings));
  } catch {
    // Private browsing or a disabled storage API must not block task creation.
  }
}

function rememberedPriority(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value)
    ? Math.min(10, Math.max(1, value))
    : 5;
}

function promptWithAttachments(prompt: string, paths: string[]): string {
  const base = prompt.trim() || "请查看附件，并根据附件内容完成任务。";
  if (paths.length === 0) return base;
  return `${base}\n\n附件（本地路径，请查看）：\n${paths.map((path) => `- ${path}`).join("\n")}`;
}

export function CreateTaskDialog({
  project,
  projects,
  onClose,
  onCreated,
}: {
  project?: Project;
  projects?: Project[];
  onClose: () => void;
  onCreated: () => void;
}) {
  const [initialSettings] = useState(rememberedTaskSettings);
  const availableProjects = projects ?? (project ? [project] : []);
  const rememberedProjectId = initialSettings.projectId;
  const initialProjectId = project?.id ?? (
    availableProjects.some((item) => item.id === rememberedProjectId)
      ? rememberedProjectId!
      : availableProjects[0]?.id ?? 0
  );
  const [selectedProjectId, setSelectedProjectId] = useState(initialProjectId);
  const selectedProject = availableProjects.find((item) => item.id === selectedProjectId);
  const availableEngines = useAvailableTaskEngines();
  const installedEngines = useInstalledEngines();
  const [orchestrations, setOrchestrations] = useState<OrchestrationTemplate[]>([]);
  const [executionMode, setExecutionMode] = useState<"single" | "orchestration">(
    initialSettings.executionMode ?? "single",
  );
  const [orchestrationId, setOrchestrationId] = useState(initialSettings.orchestrationId ?? 0);
  const [engine, setEngine] = useState<"auto" | Engine>(
    ["auto", ...TASK_ENGINE_ORDER].includes(initialSettings.engine ?? "")
      ? initialSettings.engine!
      : "auto",
  );
  const [prompt, setPrompt] = useState("");
  const [priority, setPriority] = useState(rememberedPriority(initialSettings.priority));
  const [autoApprove, setAutoApprove] = useState(initialSettings.autoApprove ?? true);
  const [attachments, setAttachments] = useState<PendingAttachment[]>([]);
  const previews = useRef<Set<string>>(new Set());
  const { busy, run } = useSingleFlight();
  const selectedOrchestration = orchestrations.find((item) => item.id === orchestrationId);
  const isBrowser = executionMode === "single" && engine === "browser";

  useEffect(() => {
    let alive = true;
    api.orchestrations.list().then((items) => {
      if (!alive) return;
      const enabled = items.filter((item) => item.enabled);
      setOrchestrations(enabled);
      setOrchestrationId((current) => enabled.some((item) => item.id === current) ? current : enabled[0]?.id ?? 0);
      if (enabled.length === 0) setExecutionMode("single");
    }).catch(() => {
      if (alive) setExecutionMode("single");
    });
    return () => { alive = false; };
  }, []);

  useEffect(() => () => {
    previews.current.forEach((preview) => URL.revokeObjectURL(preview));
  }, []);

  // 记住的引擎没装(或只剩一个引擎时的「自动」)会落到不存在的选项上，纠正回可选项
  const availableKey = availableEngines.join(",");
  useEffect(() => {
    const engines = availableKey ? (availableKey.split(",") as Engine[]) : [];
    if (engines.length === 0) return;
    setEngine((current) => {
      if (engines.length === 1) return engines[0];
      return current === "auto" || engines.includes(current) ? current : "auto";
    });
  }, [availableKey]);

  function addAttachments(files: File[]) {
    if (files.length === 0) return;
    setAttachments((current) => {
      const known = new Set(current.map(({ file }) => `${file.name}:${file.size}:${file.lastModified}`));
      const additions = files
        .filter((file) => !known.has(`${file.name}:${file.size}:${file.lastModified}`))
        .map((file) => {
          const preview = file.type.startsWith("image/") ? URL.createObjectURL(file) : undefined;
          if (preview) previews.current.add(preview);
          return { id: `${Date.now()}-${Math.random()}`, file, preview };
        });
      return [...current, ...additions];
    });
  }

  function removeAttachment(id: string) {
    setAttachments((current) => current.filter((attachment) => {
      if (attachment.id !== id) return true;
      if (attachment.preview) {
        URL.revokeObjectURL(attachment.preview);
        previews.current.delete(attachment.preview);
      }
      return false;
    }));
  }

  function clearAttachments() {
    setAttachments((current) => {
      current.forEach((attachment) => {
        if (attachment.preview) URL.revokeObjectURL(attachment.preview);
      });
      previews.current.clear();
      return [];
    });
  }

  function selectEngine(nextEngine: "auto" | Engine) {
    setExecutionMode("single");
    if (nextEngine === "browser") clearAttachments();
    setEngine(nextEngine);
  }

  function handlePaste(event: ReactClipboardEvent<HTMLElement>) {
    if (isBrowser) return;
    const itemImages = Array.from(event.clipboardData.items)
      .filter((item) => item.kind === "file" && item.type.startsWith("image/"))
      .map((item) => item.getAsFile())
      .filter((file): file is File => file !== null);
    const pastedImages = itemImages.length > 0
      ? itemImages
      : Array.from(event.clipboardData.files).filter((file) => file.type.startsWith("image/"));
    if (pastedImages.length === 0) return;
    addAttachments(pastedImages);
    if (!event.clipboardData.getData("text/plain")) event.preventDefault();
  }

  async function submit(start: boolean) {
    await run(async () => {
      const trimmed = prompt.trim();
      const taskAttachments = isBrowser ? [] : attachments;
      if ((!trimmed && taskAttachments.length === 0) || !selectedProject) return;
      const initialPrompt = promptWithAttachments(trimmed, []);
      if (executionMode === "orchestration" && selectedOrchestration) {
        let orchestrationRun;
        try {
          orchestrationRun = await api.orchestrationRuns.create({
            orchestration_id: selectedOrchestration.id,
            project_id: selectedProject.id,
            prompt: initialPrompt,
            priority,
            auto_approve: autoApprove,
            start: false,
          });
        } catch (error) {
          toast(`创建编排任务失败：${errorMessage(error)}`, "error");
          return;
        }

        let attachmentFailures = 0;
        for (const attachment of taskAttachments) {
          try {
            await api.orchestrationRuns.uploadFile(orchestrationRun.id, attachment.file);
          } catch {
            attachmentFailures += 1;
          }
        }
        rememberTaskSettings({
          projectId: selectedProject.id,
          engine,
          priority,
          autoApprove,
          executionMode,
          orchestrationId: selectedOrchestration.id,
        });
        if (attachmentFailures > 0) {
          onCreated();
          onClose();
          toast(`编排 #${orchestrationRun.id} 已创建为待办，但有 ${attachmentFailures} 个附件未能附加`, "error");
          return;
        }
        if (start) {
          const firstEngine = selectedOrchestration.steps[0]?.engine;
          if (!firstEngine || !(await guardQuota(firstEngine))) {
            onCreated();
            onClose();
            toast(`编排 #${orchestrationRun.id} 已创建为待办，未执行`, "info");
            return;
          }
          try {
            await api.orchestrationRuns.start(orchestrationRun.id);
          } catch (error) {
            onCreated();
            onClose();
            toast(`编排 #${orchestrationRun.id} 已创建，但启动失败：${errorMessage(error)}`, "error");
            return;
          }
        }
        onCreated();
        onClose();
        toast(start ? `编排 #${orchestrationRun.id} 已排入执行` : `编排 #${orchestrationRun.id} 已加入待办`, "success");
        return;
      }
      let r: { id: number; engine: string; auto_reason: string | null };
      try {
        r = await api.createTask({
          project_id: selectedProject.id,
          engine,
          prompt: initialPrompt,
          priority,
          auto_approve: autoApprove,
          start: false,
        });
      } catch (error) {
        toast(`创建任务失败：${errorMessage(error)}`, "error");
        return;
      }

      rememberTaskSettings({
        projectId: selectedProject.id,
        engine,
        priority,
        autoApprove,
        executionMode: "single",
      });

      const uploadedPaths: string[] = [];
      let attachmentFailures = 0;
      for (const attachment of taskAttachments) {
        try {
          const uploaded = await api.uploadFile(r.id, attachment.file);
          uploadedPaths.push(uploaded.path);
        } catch {
          attachmentFailures += 1;
        }
      }
      if (uploadedPaths.length > 0) {
        try {
          await api.updateTask(r.id, { prompt: promptWithAttachments(trimmed, uploadedPaths) });
        } catch {
          attachmentFailures += uploadedPaths.length;
        }
      }

      if (r.auto_reason) toast(`🤖 自动选了 ${engineName(r.engine)}：${r.auto_reason}`, "info");
      onCreated();
      onClose();

      if (attachmentFailures > 0) {
        toast(`任务 #${r.id} 已创建为待办，但有 ${attachmentFailures} 个附件未能附加，请打开任务后重试`, "error");
        return;
      }

      if (!start) return;
      try {
        if (!(await guardQuota(r.engine))) {
          toast(`任务 #${r.id} 已创建为待办，未执行`, "info");
          return;
        }
        await api.startTask(r.id);
        const latest = await api.getTask(r.id);
        onCreated();
        if (latest.status === "failed") {
          toast(`任务 #${r.id} 已创建，但启动失败；打开任务查看日志`, "error");
          return;
        }
        toast(`任务 #${r.id} 已排入执行`, "success");
      } catch (error) {
        toast(`任务 #${r.id} 已创建，但启动失败：${errorMessage(error)}`, "error");
      }
    });
  }

  return (
    <Modal title={project ? `新任务 · ${project.name}` : "新建任务"} onClose={onClose}>
      <div className="space-y-3 text-sm" onPaste={handlePaste}>
        {projects && (
          <label className="block">
            <span className="mb-1.5 block text-xs font-medium text-dh-tsoft">项目</span>
            <select
              className="w-full rounded-lg border border-dh-bsoft bg-dh-surface px-3 py-2 text-sm text-dh-text outline-none focus:border-dh-m2"
              value={selectedProjectId}
              onChange={(event) => setSelectedProjectId(Number(event.target.value))}
            >
              {availableProjects.map((item) => (
                <option key={item.id} value={item.id}>{item.name} · {item.path}</option>
              ))}
            </select>
          </label>
        )}
        <div className="flex flex-wrap gap-3">
          {availableEngines.length > 1 && (
            <label className="flex items-center gap-1.5" title="按配额余量+历史成功率自动选">
              <input type="radio" checked={executionMode === "single" && engine === "auto"} onChange={() => selectEngine("auto")} />
              🤖 自动
            </label>
          )}
          {availableEngines.map((item) => (
            <label key={item} className="flex items-center gap-1.5">
              <input type="radio" checked={executionMode === "single" && engine === item} onChange={() => selectEngine(item)} />
              {engineName(item)}
            </label>
          ))}
          {orchestrations.length > 0 && (
            <label className="flex items-center gap-1.5">
              <input
                type="radio"
                checked={executionMode === "orchestration"}
                onChange={() => setExecutionMode("orchestration")}
              />
              🔗 编排
            </label>
          )}
        </div>
        {executionMode === "orchestration" && orchestrations.length > 0 && (
          <label className="block rounded-lg border border-dh-bsoft bg-dh-soft p-3">
            <span className="mb-1.5 block text-xs font-medium text-dh-tsoft">选择编排</span>
            <select
              value={orchestrationId}
              onChange={(event) => setOrchestrationId(Number(event.target.value))}
              className="w-full rounded-lg border border-dh-bsoft bg-dh-surface px-3 py-2 text-sm text-dh-text"
            >
              {orchestrations.map((item) => {
                const missing = item.steps.filter((step) => installedEngines?.[step.engine] === false);
                return <option key={item.id} value={item.id} disabled={missing.length > 0}>
                  {item.name} · {item.steps.map((step) => step.name).join(" → ")}
                  {missing.length > 0 ? `（缺少 ${missing.map((step) => engineName(step.engine)).join("、")}）` : ""}
                </option>;
              })}
            </select>
            {selectedOrchestration && (
              <span className="mt-1.5 block text-[11px] text-slate-400">
                {selectedOrchestration.steps.map((step) => (
                  `${engineName(step.engine)} · ${step.name}${step.model ? ` · ${step.model}` : ""}${step.reasoning_effort ? ` · ${step.reasoning_effort}` : ""}`
                )).join(" → ")}
              </span>
            )}
          </label>
        )}
        <textarea
          className="h-32 w-full rounded-lg border border-dh-bsoft bg-dh-soft p-2.5 font-mono text-xs text-dh-text focus:border-dh-m2 focus:outline-none"
          placeholder={isBrowser
            ? "输入本地 URL 和验收目标，例如：检查 http://127.0.0.1:5199 的登录表单"
            : `给 ${availableEngines.filter((item) => item !== "browser").join("/")} 的指令…`}
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          // 触屏不 autoFocus：iOS/WebKit 对动态插入弹窗里的 autofocus 输入框会「键盘闪现即收、
          // 元素却保持聚焦」，之后点击已聚焦元素 focus() 是空操作，键盘要点很多次才弹得出来。
          autoFocus={!projects && !isCoarsePointer()}
        />
        {!isBrowser && <div className="rounded-lg border-2 border-dashed border-dh-accent bg-dh-soft p-3 shadow-inner shadow-black/20">
          <div className="flex items-center gap-2">
            <span className="text-xs font-medium text-slate-100">📋 粘贴截图到这里（Ctrl/⌘ + V）</span>
            <AttachmentPicker
              accept="image/*"
              buttonClassName="ml-auto rounded-md border border-dh-accent bg-dh-accent px-2.5 py-1 text-xs font-medium text-dh-accfg hover:bg-dh-acchov"
              onFiles={addAttachments}
            >
              + 选择图片
            </AttachmentPicker>
            <AttachmentPicker
              buttonClassName="rounded-md border border-dh-border bg-dh-s2 px-2.5 py-1 text-xs font-medium text-slate-100 hover:bg-dh-hover"
              onFiles={addAttachments}
            >
              + 选择文件
            </AttachmentPicker>
          </div>
          {attachments.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-2">
              {attachments.map((attachment) => (
                attachment.preview ? (
                  <div key={attachment.id} className="group relative h-16 w-16 overflow-hidden rounded-md border-2 border-dh-border bg-dh-s2 shadow-sm">
                    <img src={attachment.preview} alt={attachment.file.name} className="h-full w-full object-cover" />
                    <button
                      type="button"
                      className="absolute right-0.5 top-0.5 rounded bg-rose-600 px-1.5 text-xs font-bold text-white shadow hover:bg-rose-500"
                      onClick={() => removeAttachment(attachment.id)}
                      aria-label={`移除 ${attachment.file.name}`}
                    >
                      ×
                    </button>
                  </div>
                ) : (
                  <div
                    key={attachment.id}
                    className="inline-flex h-16 max-w-40 items-center gap-1.5 rounded-md border-2 border-dh-border bg-dh-s2 px-2 text-xs text-slate-100 shadow-sm"
                    title={attachment.file.name}
                  >
                    <span className="min-w-0 flex-1 truncate">📎 {attachment.file.name}</span>
                    <button
                      type="button"
                      className="shrink-0 rounded bg-rose-600 px-1.5 font-bold text-white shadow hover:bg-rose-500"
                      onClick={() => removeAttachment(attachment.id)}
                      aria-label={`移除 ${attachment.file.name}`}
                    >
                      ×
                    </button>
                  </div>
                )
              ))}
            </div>
          )}
        </div>}
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
          <label className="flex items-center gap-2 whitespace-nowrap">
            优先级
            <input
              type="number"
              min={1}
              max={10}
              className="w-16 rounded-lg border border-dh-bsoft bg-dh-soft px-2 py-1 text-dh-text"
              value={priority}
              onChange={(e) => setPriority(Number(e.target.value))}
            />
          </label>
          {!isBrowser && <label
            className={`flex items-center gap-2 rounded-lg border px-2.5 py-1.5 text-sm ${
              autoApprove ? "border-amber-500/40 bg-amber-500/10 text-amber-400" : "border-dh-bsoft text-dh-tsoft"
            }`}
            title={availableEngines.map((item) => `${item}: ${AUTO_APPROVE_FLAG[item]}`).join(" / ")}
          >
            <input
              type="checkbox"
              checked={autoApprove}
              onChange={(e) => setAutoApprove(e.target.checked)}
            />
            <span className="whitespace-nowrap">⚡ 全权限运行（跳过确认）</span>
          </label>}
          {isBrowser && (
            <span className="rounded-lg border border-cyan-500/30 bg-cyan-500/10 px-2.5 py-1.5 text-xs text-cyan-300">
              仅访问本机回环地址 · 危险动作始终确认
            </span>
          )}
        </div>
        <div className="flex flex-col gap-2 pt-2">
          <div className="flex items-center gap-2">
            <button
              className="ml-auto shrink-0 rounded-lg border border-dh-bsoft px-3 py-1.5 text-dh-tsoft hover:bg-dh-hover"
              onClick={onClose}
            >
              取消
            </button>
            <button
              className="shrink-0 rounded-lg bg-dh-accent px-3 py-1.5 font-medium text-dh-accfg hover:bg-dh-acchov disabled:opacity-50"
              disabled={busy || !selectedProject || (!prompt.trim() && (isBrowser || attachments.length === 0))}
              onClick={() => submit(false)}
            >
              {busy ? "处理中…" : "加入待办"}
            </button>
            <button
              className="shrink-0 rounded-lg bg-emerald-500 px-3 py-1.5 font-medium text-white hover:bg-emerald-600 disabled:opacity-50"
              disabled={busy || !selectedProject || (!prompt.trim() && (isBrowser || attachments.length === 0))}
              onClick={() => submit(true)}
              title="创建并立即排入调度执行"
            >
              {busy ? "处理中…" : "创建并执行"}
            </button>
          </div>
        </div>
      </div>
    </Modal>
  );
}
