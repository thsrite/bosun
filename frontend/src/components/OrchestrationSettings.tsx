import { useCallback, useEffect, useState } from "react";
import { api, type AppSettings } from "../api";
import { engineName } from "../engines";
import { useAvailableEngines } from "../installedEngines";
import { confirmDialog, toast } from "../overlay";
import type { Engine, OrchestrationTemplate } from "../types";
import { useSingleFlight } from "../useSingleFlight";
import { ModelCombobox, type ModelOption } from "./ModelCombobox";

type CodingEngine = Exclude<Engine, "browser">;

type DraftStep = {
  key: string;
  name: string;
  engine: CodingEngine;
  model: string;
  reasoning_effort: string;
  role_prompt: string;
};

type Draft = {
  id: number | null;
  name: string;
  enabled: boolean;
  steps: DraftStep[];
};

function emptyStep(engine: CodingEngine, name: string, rolePrompt: string): DraftStep {
  return {
    key: crypto.randomUUID(),
    name,
    engine,
    model: "",
    reasoning_effort: "",
    role_prompt: rolePrompt,
  };
}

function emptyDraft(engines: CodingEngine[]): Draft {
  const first = engines[0] ?? "cc";
  const second = engines[1] ?? first;
  return {
    id: null,
    name: "",
    enabled: true,
    steps: [
      emptyStep(first, "方案负责人", "分析原始任务并输出清晰、可执行的方案。"),
      emptyStep(second, "实施负责人", "结合原始任务和前序产物完成实现与验证。"),
    ],
  };
}

function toDraft(template: OrchestrationTemplate): Draft {
  return {
    id: template.id,
    name: template.name,
    enabled: template.enabled,
    steps: template.steps.map(({ id, name, engine, model, reasoning_effort, role_prompt }) => ({
      key: `saved-${id}`,
      name,
      engine,
      model,
      reasoning_effort,
      role_prompt,
    })),
  };
}

function modelConfig(settings: AppSettings, engine: CodingEngine): { value: string; options: ModelOption[] } {
  if (engine === "cc") return { value: settings.claude_model, options: settings.claude_model_options };
  if (engine === "codex") return { value: settings.codex_model, options: settings.codex_model_options };
  if (engine === "omp") return { value: settings.omp_model, options: settings.omp_model_options };
  return { value: settings.kimi_model, options: settings.kimi_model_options };
}

function reasoningConfig(settings: AppSettings, engine: CodingEngine) {
  if (engine === "cc") return { value: settings.claude_effort, options: settings.claude_effort_options };
  if (engine === "codex") return { value: settings.codex_effort, options: settings.codex_effort_options };
  if (engine === "omp") return { value: settings.omp_thinking, options: settings.omp_thinking_options };
  return null;
}

function inheritedLabel(value: string): string {
  return value ? `继承全局设置（${value}）` : "继承全局设置";
}

export function OrchestrationSettings({ settings }: { settings: AppSettings }) {
  const engines = useAvailableEngines().filter((engine): engine is CodingEngine => engine !== "browser");
  const [templates, setTemplates] = useState<OrchestrationTemplate[]>([]);
  const [draft, setDraft] = useState<Draft | null>(null);
  const { busy, run } = useSingleFlight();

  const load = useCallback(async () => {
    setTemplates(await api.orchestrations.list());
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  function patchStep(index: number, patch: Partial<DraftStep>) {
    setDraft((current) => current ? {
      ...current,
      steps: current.steps.map((step, stepIndex) => stepIndex === index ? { ...step, ...patch } : step),
    } : current);
  }

  function moveStep(index: number, direction: -1 | 1) {
    setDraft((current) => {
      if (!current) return current;
      const target = index + direction;
      if (target < 0 || target >= current.steps.length) return current;
      const steps = [...current.steps];
      [steps[index], steps[target]] = [steps[target], steps[index]];
      return { ...current, steps };
    });
  }

  async function save() {
    if (!draft) return;
    await run(async () => {
      const body = {
        name: draft.name,
        enabled: draft.enabled,
        steps: draft.steps.map(({ name, engine, model, reasoning_effort, role_prompt }) => ({
          name,
          engine,
          model,
          reasoning_effort,
          role_prompt,
        })),
      };
      try {
        if (draft.id == null) await api.orchestrations.create(body);
        else await api.orchestrations.update(draft.id, body);
        await load();
        setDraft(null);
        toast("编排已保存", "success");
      } catch (error) {
        toast(`保存编排失败：${error instanceof Error ? error.message : String(error)}`, "error");
      }
    });
  }

  async function remove(template: OrchestrationTemplate) {
    if (!await confirmDialog(`删除编排「${template.name}」？历史运行不受影响。`, { danger: true })) return;
    await run(async () => {
      await api.orchestrations.remove(template.id);
      if (draft?.id === template.id) setDraft(null);
      await load();
    });
  }

  return (
    <section className="card min-w-0 p-4 lg:p-5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 className="text-sm font-semibold text-dh-text">任务编排</h2>
          <p className="mt-1 text-xs text-slate-400">按顺序运行多个自定义角色；角色提示词会与原任务、前序产物一起注入 CLI。</p>
        </div>
        <button
          type="button"
          onClick={() => setDraft(emptyDraft(engines))}
          className="rounded-lg bg-dh-accent px-3 py-1.5 text-sm font-medium text-dh-accfg hover:bg-dh-acchov"
        >+ 新建编排</button>
      </div>

      {templates.length === 0 && !draft ? (
        <div className="mt-4 rounded-lg border border-dashed border-dh-bsoft px-4 py-6 text-center text-xs text-slate-400">
          尚未配置编排；新建任务界面暂不会显示“编排”。
        </div>
      ) : (
        <div className="mt-4 space-y-2">
          {templates.map((template) => (
            <div key={template.id} className="flex flex-wrap items-center gap-2 rounded-lg border border-dh-bsoft bg-dh-soft px-3 py-2">
              <span className={`h-2 w-2 rounded-full ${template.enabled ? "bg-emerald-400" : "bg-slate-500"}`} />
              <span className="font-medium text-dh-text">{template.name}</span>
              <span className="min-w-0 flex-1 truncate text-xs text-slate-400">
                {template.steps.map((step) => `${engineName(step.engine)} · ${step.name}${step.model ? ` · ${step.model}` : ""}`).join(" → ")}
              </span>
              <button type="button" onClick={() => setDraft(toDraft(template))} className="rounded px-2 py-1 text-xs text-dh-tsoft hover:bg-dh-hover">编辑</button>
              <button type="button" onClick={() => void remove(template)} className="rounded px-2 py-1 text-xs text-rose-400 hover:bg-rose-500/20">删除</button>
            </div>
          ))}
        </div>
      )}

      {draft && (
        <div className="mt-4 space-y-3 rounded-xl border border-dh-border bg-dh-soft p-3">
          <div className="flex flex-wrap items-center gap-3">
            <label className="min-w-0 flex-1">
              <span className="mb-1 block text-xs text-dh-muted">编排名称</span>
              <input value={draft.name} onChange={(event) => setDraft({ ...draft, name: event.target.value })} className="w-full rounded-lg border border-dh-bsoft bg-dh-surface px-3 py-2 text-sm" />
            </label>
            <label className="mt-5 flex items-center gap-2 text-xs text-dh-tsoft">
              <input type="checkbox" checked={draft.enabled} onChange={(event) => setDraft({ ...draft, enabled: event.target.checked })} />启用
            </label>
          </div>

          {draft.steps.map((step, index) => {
            const models = modelConfig(settings, step.engine);
            const reasoning = reasoningConfig(settings, step.engine);
            return <div key={step.key} className="rounded-lg border border-dh-bsoft bg-dh-surface p-3">
              <div className="flex flex-wrap items-end gap-2">
                <label className="min-w-40 flex-1">
                  <span className="mb-1 block text-xs text-dh-muted">角色名称</span>
                  <input value={step.name} onChange={(event) => patchStep(index, { name: event.target.value })} className="w-full rounded-md border border-dh-bsoft bg-dh-soft px-2.5 py-1.5 text-sm" />
                </label>
                <label>
                  <span className="mb-1 block text-xs text-dh-muted">执行 CLI</span>
                  <select value={step.engine} onChange={(event) => patchStep(index, { engine: event.target.value as CodingEngine, model: "", reasoning_effort: "" })} className="rounded-md border border-dh-bsoft bg-dh-soft px-2.5 py-1.5 text-sm">
                    {engines.map((engine) => <option key={engine} value={engine}>{engineName(engine)}</option>)}
                  </select>
                </label>
                <div className="flex gap-1">
                  <button type="button" disabled={index === 0} onClick={() => moveStep(index, -1)} className="rounded border border-dh-bsoft px-2 py-1 disabled:opacity-30">↑</button>
                  <button type="button" disabled={index === draft.steps.length - 1} onClick={() => moveStep(index, 1)} className="rounded border border-dh-bsoft px-2 py-1 disabled:opacity-30">↓</button>
                  <button type="button" disabled={draft.steps.length <= 2} onClick={() => setDraft({ ...draft, steps: draft.steps.filter((_, stepIndex) => stepIndex !== index) })} className="rounded border border-rose-500/30 px-2 py-1 text-rose-400 disabled:opacity-30">移除</button>
                </div>
              </div>
              <div className="mt-2 flex flex-wrap gap-2">
                <label>
                  <span className="mb-1 block text-xs text-dh-muted">模型</span>
                  <ModelCombobox
                    id={`orchestration-step-${step.key}-model`}
                    value={step.model}
                    options={models.options}
                    placeholder={inheritedLabel(models.value)}
                    onChange={(model) => patchStep(index, { model })}
                    onCommit={(model) => patchStep(index, { model: model.trim() })}
                  />
                </label>
                {reasoning && <label>
                  <span className="mb-1 block text-xs text-dh-muted">{step.engine === "omp" ? "思考强度" : "推理强度"}</span>
                  <select
                    value={step.reasoning_effort}
                    onChange={(event) => patchStep(index, { reasoning_effort: event.target.value })}
                    className="w-44 rounded-md border border-dh-bsoft bg-dh-soft px-2.5 py-1.5 text-sm"
                  >
                    {reasoning.options.map((option) => (
                      <option key={option.value || "inherit"} value={option.value}>
                        {option.value ? option.label : inheritedLabel(reasoning.value)}
                      </option>
                    ))}
                  </select>
                </label>}
              </div>
              <label className="mt-2 block">
                <span className="mb-1 block text-xs text-dh-muted">角色提示词</span>
                <textarea value={step.role_prompt} onChange={(event) => patchStep(index, { role_prompt: event.target.value })} className="h-20 w-full rounded-md border border-dh-bsoft bg-dh-soft p-2 text-xs" />
              </label>
            </div>;
          })}

          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              disabled={draft.steps.length >= 5 || engines.length === 0}
              onClick={() => setDraft({ ...draft, steps: [...draft.steps, emptyStep(engines[0] ?? "cc", "新角色", "根据原始任务和前序产物完成当前角色职责。")] })}
              className="rounded-lg border border-dh-bsoft px-3 py-1.5 text-xs text-dh-tsoft hover:bg-dh-hover disabled:opacity-40"
            >+ 增加角色</button>
            <button type="button" onClick={() => setDraft(null)} className="ml-auto rounded-lg border border-dh-bsoft px-3 py-1.5 text-sm text-dh-tsoft hover:bg-dh-hover">取消</button>
            <button type="button" disabled={busy || !draft.name.trim() || draft.steps.some((step) => !step.name.trim() || !step.role_prompt.trim())} onClick={() => void save()} className="rounded-lg bg-dh-accent px-3 py-1.5 text-sm font-medium text-dh-accfg hover:bg-dh-acchov disabled:opacity-40">{busy ? "保存中…" : "保存编排"}</button>
          </div>
        </div>
      )}
    </section>
  );
}
