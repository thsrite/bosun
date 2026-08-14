import { DndContext, PointerSensor, closestCenter, useSensor, useSensors, type DragEndEvent } from "@dnd-kit/core";
import { SortableContext, arrayMove, horizontalListSortingStrategy, useSortable } from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import { useCallback, useEffect, useState } from "react";
import { api, type AppSettings } from "../api";
import { engineBadgeClass, engineName, engineShort } from "../engines";
import { useAvailableEngines } from "../installedEngines";
import {
  BUILT_IN_ORCHESTRATIONS,
  BUILT_IN_ROLES,
  applyRolePreset,
  getBuiltInRole,
  nextAvailableOrchestrationName,
  type BuiltInOrchestration,
  type BuiltInRoleId,
} from "../orchestrationPresets";
import { confirmDialog, toast } from "../overlay";
import type { Engine, OrchestrationTemplate } from "../types";
import { useSingleFlight } from "../useSingleFlight";
import { ModelCombobox, type ModelOption } from "./ModelCombobox";

type CodingEngine = Exclude<Engine, "browser">;

const MIN_STEPS = 2;
const MAX_STEPS = 5;

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
  const first = engines[0] ?? "claude";
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

function draftFromPreset(preset: BuiltInOrchestration, engines: CodingEngine[], existingNames: readonly string[]): Draft {
  const fallbackEngine = engines[0] ?? "claude";
  return {
    id: null,
    name: nextAvailableOrchestrationName(preset.name, existingNames),
    enabled: true,
    steps: preset.steps.map(({ roleId, preferredEngine }) => {
      const role = getBuiltInRole(roleId);
      return emptyStep(
        engines.includes(preferredEngine) ? preferredEngine : fallbackEngine,
        role.name,
        role.rolePrompt,
      );
    }),
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
  if (engine === "claude") return { value: settings.claude_model, options: settings.claude_model_options };
  if (engine === "codex") return { value: settings.codex_model, options: settings.codex_model_options };
  if (engine === "omp") return { value: settings.omp_model, options: settings.omp_model_options };
  return { value: settings.kimi_model, options: settings.kimi_model_options };
}

function reasoningConfig(settings: AppSettings, engine: CodingEngine) {
  if (engine === "claude") return { value: settings.claude_effort, options: settings.claude_effort_options };
  if (engine === "codex") return { value: settings.codex_effort, options: settings.codex_effort_options };
  if (engine === "omp") return { value: settings.omp_thinking, options: settings.omp_thinking_options };
  return null;
}

function inheritedLabel(value: string): string {
  return value ? `继承全局设置（${value}）` : "继承全局设置";
}

function stepIncomplete(step: DraftStep): boolean {
  return !step.name.trim() || !step.role_prompt.trim();
}

/** 流水线上的一张角色节点卡：只负责展示与选中，编辑在下方详情面板里做。 */
function StepNode({
  step,
  index,
  selected,
  onSelect,
}: {
  step: DraftStep;
  index: number;
  selected: boolean;
  onSelect: () => void;
}) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({ id: step.key });
  const incomplete = stepIncomplete(step);
  return (
    <button
      ref={setNodeRef}
      type="button"
      style={{ transform: CSS.Transform.toString(transform), transition }}
      onClick={onSelect}
      {...attributes}
      {...listeners}
      className={`w-52 shrink-0 cursor-grab rounded-xl border bg-dh-surface p-3 text-left transition active:cursor-grabbing ${
        selected ? "border-dh-accent shadow-[0_0_0_3px_rgb(var(--dh-accent-rgb)/0.16)]" : "border-dh-bsoft hover:border-dh-border"
      } ${isDragging ? "z-10 opacity-70" : ""}`}
    >
      <div className="flex items-center gap-2">
        <span className={`flex h-5 w-5 items-center justify-center rounded-full text-[11px] font-semibold ${
          selected ? "bg-dh-accent text-dh-accfg" : "bg-dh-s2 text-dh-muted"
        }`}>{index + 1}</span>
        <span className={`min-w-0 flex-1 truncate text-sm font-medium ${step.name.trim() ? "text-dh-text" : "text-dh-muted-2"}`}>
          {step.name.trim() || "未命名角色"}
        </span>
        {incomplete && <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-rose-400" title="角色名称与提示词都必须填写" />}
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-1">
        <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${engineBadgeClass(step.engine)}`}>{engineShort(step.engine)}</span>
        <span className="rounded bg-dh-s2 px-1.5 py-0.5 text-[10px] text-dh-muted">{step.model || "继承模型"}</span>
        {step.reasoning_effort && <span className="rounded bg-dh-s2 px-1.5 py-0.5 text-[10px] text-dh-muted">{step.reasoning_effort}</span>}
      </div>
      <p className="mt-2 line-clamp-2 text-[11px] leading-relaxed text-slate-400">
        {step.role_prompt.trim() || "（未填写角色提示词）"}
      </p>
    </button>
  );
}

/** 节点之间的连接线；悬停时露出 ⊕，就地插入一个角色。 */
function Connector({ onInsert, disabled }: { onInsert: () => void; disabled: boolean }) {
  return (
    <div className="group/con relative flex h-full shrink-0 items-center px-1">
      <span className="h-px w-6 bg-dh-border" />
      <span className="-ml-1 text-dh-border">▶</span>
      <button
        type="button"
        disabled={disabled}
        onClick={onInsert}
        title={disabled ? `最多 ${MAX_STEPS} 个角色` : "在此插入角色"}
        className="absolute left-1/2 top-1/2 hidden h-5 w-5 -translate-x-1/2 -translate-y-1/2 items-center justify-center rounded-full border border-dh-accent bg-dh-surface text-xs leading-none text-dh-accent group-hover/con:flex disabled:hidden"
      >+</button>
    </div>
  );
}

export function OrchestrationSettings({ settings }: { settings: AppSettings }) {
  const engines = useAvailableEngines().filter((engine): engine is CodingEngine => engine !== "browser");
  const [templates, setTemplates] = useState<OrchestrationTemplate[]>([]);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const { busy, run } = useSingleFlight();
  const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 4 } }));
  const developmentPreset = BUILT_IN_ORCHESTRATIONS[0];

  const load = useCallback(async () => {
    setTemplates(await api.orchestrations.list());
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  function openDraft(next: Draft) {
    setDraft(next);
    setSelectedKey(next.steps[0]?.key ?? null);
  }

  function patchStep(index: number, patch: Partial<DraftStep>) {
    setDraft((current) => current ? {
      ...current,
      steps: current.steps.map((step, stepIndex) => stepIndex === index ? { ...step, ...patch } : step),
    } : current);
  }

  function fillFromRolePreset(index: number, roleId: BuiltInRoleId) {
    setDraft((current) => current ? {
      ...current,
      steps: current.steps.map((step, stepIndex) => stepIndex === index ? applyRolePreset(step, roleId) : step),
    } : current);
  }

  function insertStep(at: number) {
    setDraft((current) => {
      if (!current || current.steps.length >= MAX_STEPS) return current;
      const step = emptyStep(engines[0] ?? "claude", "新角色", "根据原始任务和前序产物完成当前角色职责。");
      setSelectedKey(step.key);
      return { ...current, steps: [...current.steps.slice(0, at), step, ...current.steps.slice(at)] };
    });
  }

  function removeStep(index: number) {
    setDraft((current) => {
      if (!current || current.steps.length <= MIN_STEPS) return current;
      const steps = current.steps.filter((_, stepIndex) => stepIndex !== index);
      setSelectedKey(steps[Math.min(index, steps.length - 1)].key);
      return { ...current, steps };
    });
  }

  function onDragEnd(event: DragEndEvent) {
    const { active, over } = event;
    if (!over || active.id === over.id) return;
    setDraft((current) => {
      if (!current) return current;
      const from = current.steps.findIndex((step) => step.key === active.id);
      const to = current.steps.findIndex((step) => step.key === over.id);
      if (from < 0 || to < 0) return current;
      return { ...current, steps: arrayMove(current.steps, from, to) };
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

  const selectedIndex = draft ? draft.steps.findIndex((step) => step.key === selectedKey) : -1;
  const selected = selectedIndex >= 0 && draft ? draft.steps[selectedIndex] : null;
  const models = selected ? modelConfig(settings, selected.engine) : null;
  const reasoning = selected ? reasoningConfig(settings, selected.engine) : null;

  return (
    <section className="card min-w-0 p-4 lg:p-5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 className="text-sm font-semibold text-dh-text">任务编排</h2>
          <p className="mt-1 text-xs text-slate-400">按顺序运行多个自定义角色；角色提示词会与原任务、前序产物一起注入 CLI。</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            disabled={engines.length === 0}
            onClick={() => openDraft(draftFromPreset(developmentPreset, engines, templates.map((template) => template.name)))}
            title={developmentPreset.description}
            className="rounded-lg border border-dh-accent/50 px-3 py-1.5 text-sm font-medium text-dh-accent hover:bg-dh-accent/10 disabled:opacity-40"
          >使用内置“{developmentPreset.name}”</button>
          <button
            type="button"
            onClick={() => openDraft(emptyDraft(engines))}
            className="rounded-lg bg-dh-accent px-3 py-1.5 text-sm font-medium text-dh-accfg hover:bg-dh-acchov"
          >+ 新建编排</button>
        </div>
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
              <button type="button" onClick={() => openDraft(toDraft(template))} className="rounded px-2 py-1 text-xs text-dh-tsoft hover:bg-dh-hover">编辑</button>
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

          <div className="flex items-center justify-between text-xs text-dh-muted">
            <span>流水线（{draft.steps.length}/{MAX_STEPS} 个角色）</span>
            <span>拖拽卡片可排序 · 点卡片编辑 · 连接线上的 + 可插入角色</span>
          </div>

          <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={onDragEnd}>
            <SortableContext items={draft.steps.map((step) => step.key)} strategy={horizontalListSortingStrategy}>
              <div className="dh-scrollbar-none -mx-1 flex items-stretch overflow-x-auto px-1 pb-1">
                {draft.steps.map((step, index) => (
                  <div key={step.key} className="flex items-stretch">
                    {index > 0 && <Connector onInsert={() => insertStep(index)} disabled={draft.steps.length >= MAX_STEPS} />}
                    <StepNode step={step} index={index} selected={step.key === selectedKey} onSelect={() => setSelectedKey(step.key)} />
                  </div>
                ))}
                <div className="flex items-stretch">
                  <Connector onInsert={() => insertStep(draft.steps.length)} disabled={draft.steps.length >= MAX_STEPS} />
                  <button
                    type="button"
                    disabled={draft.steps.length >= MAX_STEPS || engines.length === 0}
                    onClick={() => insertStep(draft.steps.length)}
                    className="w-32 shrink-0 rounded-xl border border-dashed border-dh-bsoft text-xs text-dh-muted hover:border-dh-accent hover:text-dh-accent disabled:opacity-30 disabled:hover:border-dh-bsoft disabled:hover:text-dh-muted"
                  >+ 增加角色</button>
                </div>
              </div>
            </SortableContext>
          </DndContext>

          {selected && models && (
            <div className="rounded-lg border border-dh-bsoft bg-dh-surface p-3">
              <div className="mb-3 flex flex-wrap items-end gap-2 rounded-lg border border-dh-bsoft bg-dh-soft p-2.5">
                <label className="min-w-56">
                  <span className="mb-1 block text-xs text-dh-muted">从内置角色填充</span>
                  <select
                    value=""
                    onChange={(event) => {
                      const roleId = event.target.value as BuiltInRoleId;
                      if (roleId) fillFromRolePreset(selectedIndex, roleId);
                    }}
                    className="w-full rounded-md border border-dh-bsoft bg-dh-surface px-2.5 py-1.5 text-sm"
                  >
                    <option value="">选择角色预设…</option>
                    {BUILT_IN_ROLES.map((role) => (
                      <option key={role.id} value={role.id}>{role.name} · {role.description}</option>
                    ))}
                  </select>
                </label>
                <p className="pb-1 text-xs text-dh-muted">仅填充角色名称和提示词，不改变 CLI、模型与推理强度；填充后可继续编辑。</p>
              </div>
              <div className="flex flex-wrap items-end gap-2">
                <label className="min-w-40 flex-1">
                  <span className="mb-1 block text-xs text-dh-muted">第 {selectedIndex + 1} 步角色名称</span>
                  <input value={selected.name} onChange={(event) => patchStep(selectedIndex, { name: event.target.value })} className="w-full rounded-md border border-dh-bsoft bg-dh-soft px-2.5 py-1.5 text-sm" />
                </label>
                <label>
                  <span className="mb-1 block text-xs text-dh-muted">执行 CLI</span>
                  <select value={selected.engine} onChange={(event) => patchStep(selectedIndex, { engine: event.target.value as CodingEngine, model: "", reasoning_effort: "" })} className="rounded-md border border-dh-bsoft bg-dh-soft px-2.5 py-1.5 text-sm">
                    {engines.map((engine) => <option key={engine} value={engine}>{engineName(engine)}</option>)}
                  </select>
                </label>
                <button
                  type="button"
                  disabled={draft.steps.length <= MIN_STEPS}
                  onClick={() => removeStep(selectedIndex)}
                  title={draft.steps.length <= MIN_STEPS ? `至少保留 ${MIN_STEPS} 个角色` : "移除该角色"}
                  className="rounded-md border border-rose-500/30 px-2.5 py-1.5 text-xs text-rose-400 disabled:opacity-30"
                >移除</button>
              </div>
              <div className="mt-2 flex flex-wrap gap-2">
                <label>
                  <span className="mb-1 block text-xs text-dh-muted">模型</span>
                  <ModelCombobox
                    id={`orchestration-step-${selected.key}-model`}
                    value={selected.model}
                    options={models.options}
                    placeholder={inheritedLabel(models.value)}
                    onChange={(model) => patchStep(selectedIndex, { model })}
                    onCommit={(model) => patchStep(selectedIndex, { model: model.trim() })}
                  />
                </label>
                {reasoning && <label>
                  <span className="mb-1 block text-xs text-dh-muted">{selected.engine === "omp" ? "思考强度" : "推理强度"}</span>
                  <select
                    value={selected.reasoning_effort}
                    onChange={(event) => patchStep(selectedIndex, { reasoning_effort: event.target.value })}
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
                <textarea value={selected.role_prompt} onChange={(event) => patchStep(selectedIndex, { role_prompt: event.target.value })} className="h-20 w-full rounded-md border border-dh-bsoft bg-dh-soft p-2 text-xs" />
              </label>
            </div>
          )}

          <div className="flex flex-wrap items-center gap-2">
            <button type="button" onClick={() => setDraft(null)} className="ml-auto rounded-lg border border-dh-bsoft px-3 py-1.5 text-sm text-dh-tsoft hover:bg-dh-hover">取消</button>
            <button type="button" disabled={busy || !draft.name.trim() || draft.steps.some(stepIncomplete)} onClick={() => void save()} className="rounded-lg bg-dh-accent px-3 py-1.5 text-sm font-medium text-dh-accfg hover:bg-dh-acchov disabled:opacity-40">{busy ? "保存中…" : "保存编排"}</button>
          </div>
        </div>
      )}
    </section>
  );
}
