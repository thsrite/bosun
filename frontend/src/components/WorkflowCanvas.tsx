import { useMemo, useState } from "react";
import { engineName } from "../engines";
import type { Engine, WorkflowGate, WorkflowGraph, WorkflowNode } from "../types";
import { computeWorkflowLayout } from "../workflowLayout";

export type CanvasSelection =
  | { type: "node"; id: string }
  | { type: "edge"; id: string };

const STATUS_META: Record<string, { label: string; dot: string; ring: string; badge: string }> = {
  done: { label: "已完成", dot: "bg-emerald-400", ring: "border-emerald-500/40", badge: "text-emerald-400 bg-emerald-500/10" },
  passed: { label: "已通过", dot: "bg-emerald-400", ring: "border-emerald-500/40", badge: "text-emerald-400 bg-emerald-500/10" },
  running: { label: "执行中", dot: "bg-sky-400 animate-pulse", ring: "border-sky-400/70", badge: "text-sky-400 bg-sky-500/15" },
  queued: { label: "排队中", dot: "bg-sky-300", ring: "border-sky-400/40", badge: "text-sky-300 bg-sky-500/10" },
  waiting_input: { label: "待人工", dot: "bg-amber-400 animate-pulse", ring: "border-amber-400/70", badge: "text-amber-400 bg-amber-500/15" },
  rejected: { label: "门禁拦截", dot: "bg-rose-400", ring: "border-rose-500/70", badge: "text-rose-400 bg-rose-500/15" },
  rework: { label: "退回返工", dot: "bg-amber-400", ring: "border-amber-500/60", badge: "text-amber-400 bg-amber-500/15" },
  failed: { label: "失败", dot: "bg-rose-500", ring: "border-rose-500/70", badge: "text-rose-400 bg-rose-500/15" },
  offline: { label: "会话掉线", dot: "bg-rose-400", ring: "border-rose-400/60", badge: "text-rose-300 bg-rose-500/10" },
  skipped: { label: "已跳过", dot: "bg-slate-400", ring: "border-dh-bsoft opacity-75", badge: "text-slate-400 bg-slate-500/10" },
  pending: { label: "待触发", dot: "bg-slate-500/60", ring: "border-dh-bsoft", badge: "text-dh-muted bg-dh-s2" },
};

const KIND_BADGE: Record<WorkflowNode["kind"], { text: string; cls: string }> = {
  system: { text: "确定性程序", cls: "bg-teal-500/15 text-teal-300 border-teal-500/30" },
  agent: { text: "AI 代理", cls: "bg-indigo-500/15 text-indigo-300 border-indigo-500/30" },
  human: { text: "人工门禁", cls: "bg-amber-500/15 text-amber-300 border-amber-500/30" },
};

const LANE_ACCENT: Record<string, string> = {
  intake: "border-teal-500/25 bg-teal-500/[0.04] text-teal-400",
  build: "border-sky-500/25 bg-sky-500/[0.04] text-sky-400",
  deliver: "border-violet-500/25 bg-violet-500/[0.04] text-violet-400",
};

function gateDotClass(status: WorkflowGate["status"]): string {
  if (status === "passed") return "bg-emerald-400";
  if (status === "failed") return "bg-rose-400";
  if (status === "running" || status === "waiting") return "bg-amber-400";
  return "bg-slate-500/50";
}

function formatDuration(sec: number | null): string {
  if (sec == null) return "";
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return `${m}m${s ? `${s}s` : ""}`;
}

function engineLabel(node: WorkflowNode): string {
  if (node.kind === "system" || node.kind === "human") return node.subtitle;
  const known = ["claude", "codex", "omp", "kimi", "agy", "browser"];
  const base = known.includes(node.engine) ? engineName(node.engine as Engine) : node.engine;
  return node.model ? `${base} · ${node.model}` : base;
}

export function WorkflowCanvas({
  graph,
  selection,
  onSelect,
}: {
  graph: WorkflowGraph;
  selection: CanvasSelection;
  onSelect: (next: CanvasSelection) => void;
}) {
  const [compact, setCompact] = useState(false);
  const layout = useMemo(() => computeWorkflowLayout(graph), [graph]);
  const scale = compact ? 0.86 : 1;

  return (
    <div className="rounded-xl border border-dh-bsoft bg-dh-soft/60">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-dh-bsoft px-3 py-2 text-[11px]">
        <div className="flex flex-wrap items-center gap-3 text-dh-muted">
          <span className="font-semibold text-dh-text">白盒交付拓扑图</span>
          <span className="inline-flex items-center gap-1"><i className="h-2 w-2 rounded-full bg-emerald-400" />已通过</span>
          <span className="inline-flex items-center gap-1"><i className="h-2 w-2 rounded-full bg-sky-400" />执行中</span>
          <span className="inline-flex items-center gap-1"><i className="h-2 w-2 rounded-full bg-amber-400" />待人工 / 返工</span>
          <span className="inline-flex items-center gap-1"><i className="h-2 w-2 rounded-full bg-rose-400" />门禁拦截 / 失败</span>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-dh-muted">点击节点或连线查看白盒证据</span>
          <button
            type="button"
            onClick={() => setCompact((v) => !v)}
            className="rounded border border-dh-bsoft bg-dh-surface px-2 py-0.5 text-[11px] text-dh-tsoft hover:border-dh-accent"
          >
            {compact ? "100% 视图" : "紧凑视图"}
          </button>
        </div>
      </div>

      <div
        className="overflow-x-auto px-2 py-3"
        style={{
          backgroundImage: "radial-gradient(rgba(148,163,184,0.16) 1px, transparent 1px)",
          backgroundSize: "16px 16px",
        }}
      >
        <div
          className="relative origin-top-left transition-transform duration-150"
          style={{
            width: layout.width * scale,
            height: layout.height * scale,
          }}
        >
          <div
            className="relative origin-top-left"
            style={{
              width: layout.width,
              height: layout.height,
              transform: `scale(${scale})`,
            }}
          >
            {/* 泳道背景框 */}
            {layout.lanes.map((lane) => (
              <div
                key={lane.id}
                className={`pointer-events-none absolute top-2 rounded-xl border ${LANE_ACCENT[lane.id] || "border-dh-bsoft bg-dh-surface/30 text-dh-muted"}`}
                style={{
                  left: lane.x,
                  width: lane.w,
                  height: layout.height - 16,
                }}
              >
                <span className="inline-block px-3 py-1 text-[11px] font-semibold tracking-wide">
                  {lane.title}
                </span>
              </div>
            ))}

            {/* SVG 连线层 */}
            <svg
              className="pointer-events-none absolute inset-0 overflow-visible"
              width={layout.width}
              height={layout.height}
            >
              <defs>
                <marker id="wf-arrow-main" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto">
                  <path d="M 0 1 L 10 5 L 0 9 z" fill="#64748b" />
                </marker>
                <marker id="wf-arrow-active" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto">
                  <path d="M 0 1 L 10 5 L 0 9 z" fill="#38bdf8" />
                </marker>
                <marker id="wf-arrow-rework" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto">
                  <path d="M 0 1 L 10 5 L 0 9 z" fill="#f59e0b" />
                </marker>
                <marker id="wf-arrow-ask" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto">
                  <path d="M 0 1 L 10 5 L 0 9 z" fill="#a78bfa" />
                </marker>
              </defs>

              {layout.mainLines.map(({ edge, path }) => {
                const isSelected = selection.type === "edge" && selection.id === edge.id;
                const stroke = isSelected ? "#38bdf8" : edge.active ? "#38bdf8" : "#64748b";
                return (
                  <g key={edge.id}>
                    <path
                      d={path}
                      fill="none"
                      stroke={stroke}
                      strokeWidth={isSelected || edge.active ? 2.6 : 1.8}
                      strokeDasharray={edge.active ? "6 4" : undefined}
                      markerEnd={edge.active || isSelected ? "url(#wf-arrow-active)" : "url(#wf-arrow-main)"}
                    />
                  </g>
                );
              })}

              {layout.reworkArcs.map(({ edge, path }) => {
                const isRework = edge.kind === "rework";
                const isSelected = selection.type === "edge" && selection.id === edge.id;
                const stroke = isRework ? "#f59e0b" : "#a78bfa";
                return (
                  <path
                    key={edge.id}
                    d={path}
                    fill="none"
                    stroke={stroke}
                    strokeWidth={isSelected ? 2.8 : 2}
                    strokeDasharray={isRework ? "6 4" : "3 3"}
                    markerEnd={isRework ? "url(#wf-arrow-rework)" : "url(#wf-arrow-ask)"}
                  />
                );
              })}
            </svg>

            {/* 连线可点击徽章 */}
            {layout.mainLines.map(({ edge, midX, midY }) => {
              const isSelected = selection.type === "edge" && selection.id === edge.id;
              return (
                <button
                  key={edge.id + "-lbl"}
                  type="button"
                  onClick={() => onSelect({ type: "edge", id: edge.id })}
                  style={{ left: midX, top: midY }}
                  className={`absolute -translate-x-1/2 -translate-y-1/2 rounded-full border px-1.5 py-0.5 text-[10px] leading-none transition ${
                    isSelected
                      ? "border-sky-400 bg-sky-500/20 text-sky-300"
                      : "border-dh-bsoft bg-dh-surface/95 text-dh-muted hover:border-dh-accent hover:text-dh-text"
                  }`}
                >
                  {edge.label}
                </button>
              );
            })}

            {layout.reworkArcs.map(({ edge, labelX, labelY }) => {
              const isRework = edge.kind === "rework";
              const isSelected = selection.type === "edge" && selection.id === edge.id;
              return (
                <button
                  key={edge.id + "-lbl"}
                  type="button"
                  onClick={() => onSelect({ type: "edge", id: edge.id })}
                  style={{ left: labelX, top: labelY }}
                  className={`absolute -translate-x-1/2 -translate-y-1/2 rounded-full border px-2 py-0.5 text-[10px] font-medium leading-none shadow-sm transition ${
                    isRework
                      ? isSelected
                        ? "border-amber-400 bg-amber-500 text-slate-950"
                        : "border-amber-500/60 bg-amber-500/15 text-amber-300 hover:bg-amber-500/25"
                      : isSelected
                        ? "border-violet-400 bg-violet-500 text-white"
                        : "border-violet-500/60 bg-violet-500/15 text-violet-300 hover:bg-violet-500/25"
                  }`}
                >
                  {edge.label}
                </button>
              );
            })}

            {/* 节点卡片层 */}
            {layout.ordered.map(({ node, x, y, w, h }) => {
              const meta = STATUS_META[node.status] ?? STATUS_META.pending;
              const kind = KIND_BADGE[node.kind];
              const isSelected = selection.type === "node" && selection.id === node.id;
              const dur = formatDuration(node.duration_sec);
              return (
                <button
                  key={node.id}
                  type="button"
                  onClick={() => onSelect({ type: "node", id: node.id })}
                  style={{ left: x, top: y, width: w, height: h }}
                  className={`absolute flex flex-col justify-between rounded-xl border bg-dh-surface p-2.5 text-left transition ${meta.ring} ${
                    isSelected
                      ? "ring-2 ring-dh-accent shadow-lg shadow-dh-accent/15"
                      : "hover:border-dh-accent/70 hover:shadow-md"
                  }`}
                >
                  <div className="flex items-center justify-between gap-1.5">
                    <span className={`rounded border px-1.5 py-0.5 text-[10px] font-medium leading-none ${kind.cls}`}>
                      {kind.text}
                    </span>
                    <span className={`inline-flex items-center gap-1 rounded-full px-1.5 py-0.5 text-[10px] leading-none ${meta.badge}`}>
                      <i className={`h-1.5 w-1.5 rounded-full ${meta.dot}`} />
                      {meta.label}
                    </span>
                  </div>

                  <div className="min-w-0">
                    <div className="flex items-center justify-between gap-1">
                      <span className="truncate text-xs font-semibold text-dh-text">{node.title}</span>
                      {node.attempt > 0 && (
                        <span className="shrink-0 font-mono text-[10px] text-dh-muted">R{node.attempt}</span>
                      )}
                    </div>
                    <div className="mt-0.5 flex items-center justify-between gap-1 text-[10px] text-dh-muted">
                      <span className="truncate">{engineLabel(node)}</span>
                      {dur && <span className="shrink-0 font-mono">{dur}</span>}
                    </div>
                  </div>

                  {/* 白盒门禁微胶囊 */}
                  <div className="flex flex-wrap items-center gap-1 pt-1 border-t border-dh-bsoft/70">
                    {node.gates.slice(0, 4).map((g) => (
                      <span
                        key={g.id}
                        title={`${g.label}：${g.detail}`}
                        className="inline-flex items-center gap-1 rounded bg-dh-soft px-1.5 py-0.5 text-[10px] leading-none text-dh-tsoft"
                      >
                        <i className={`h-1.5 w-1.5 rounded-full ${gateDotClass(g.status)}`} />
                        {g.label}
                      </span>
                    ))}
                    {node.gates.length > 4 && (
                      <span className="text-[10px] text-dh-muted">+{node.gates.length - 4}</span>
                    )}
                  </div>
                </button>
              );
            })}
          </div>
        </div>
      </div>
    </div>
  );
}
