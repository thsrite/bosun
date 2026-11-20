import test from "node:test";
import assert from "node:assert/strict";
import { computeWorkflowLayout } from "../src/workflowLayout.ts";
import type { WorkflowGraph } from "../src/types.ts";

test("computeWorkflowLayout lays out three lanes, main edges and non-overlapping rework arcs", () => {
  const graph: WorkflowGraph = {
    lanes: [
      { id: "intake", title: "弄清需求" },
      { id: "build", title: "开发与检查" },
      { id: "deliver", title: "验收与交付" },
    ],
    nodes: [
      { id: "sys:intake", kind: "system", lane: "intake", position: 0, title: "收件", subtitle: "L1", status: "passed", attempt: 1, rework_count: 0, duration_sec: 0, engine: "system", model: "", task_id: null, gates: [] },
      { id: "step:1", kind: "agent", lane: "intake", position: 1, title: "需求 AI", subtitle: "claude", status: "done", attempt: 1, rework_count: 0, duration_sec: 12, engine: "claude", model: "", task_id: 1, gates: [] },
      { id: "step:2", kind: "agent", lane: "build", position: 2, title: "开发 AI", subtitle: "codex", status: "running", attempt: 2, rework_count: 1, duration_sec: 30, engine: "codex", model: "", task_id: 2, gates: [] },
      { id: "step:3", kind: "agent", lane: "build", position: 3, title: "质量 AI", subtitle: "claude", status: "pending", attempt: 1, rework_count: 0, duration_sec: null, engine: "claude", model: "", task_id: 3, gates: [] },
      { id: "human:signoff", kind: "human", lane: "deliver", position: 4, title: "产品验收卡", subtitle: "人工", status: "pending", attempt: 0, rework_count: 0, duration_sec: null, engine: "human", model: "", task_id: null, gates: [] },
    ],
    edges: [
      { id: "e1", source: "sys:intake", target: "step:1", kind: "handoff", label: "交接", count: 1, active: false, message_ids: [] },
      { id: "e2", source: "step:1", target: "step:2", kind: "handoff", label: "交接", count: 1, active: true, message_ids: [] },
      { id: "e3", source: "step:2", target: "step:3", kind: "handoff", label: "交接", count: 1, active: false, message_ids: [] },
      { id: "e4", source: "step:3", target: "human:signoff", kind: "handoff", label: "验收卡", count: 1, active: false, message_ids: [] },
      { id: "r1", source: "step:3", target: "step:2", kind: "rework", label: "返工 ×1", count: 1, active: true, message_ids: [10] },
      { id: "r2", source: "human:signoff", target: "step:2", kind: "rework", label: "产品退回 ×1", count: 1, active: false, message_ids: [] },
    ],
    policy: { project_id: 1, trust_level: 1, diff_integrity_gate: true, require_product_signoff: true, auto_merge_globs: [], level_code: "L1", level_name: "协助开发", level_summary: "", updated_at: null },
    merge_policy: { mode: "human_merge", auto_merge: false, requires_human: true, reason: "人工合并" },
    signoffs: [],
    candidate_version: "",
    baseline_version: "",
    summary: { total_gates: 0, passed_gates: 0, failed_gates: 0, rework_total: 1 },
  };

  const layout = computeWorkflowLayout(graph);
  assert.equal(layout.lanes.length, 3);
  assert.equal(layout.ordered.length, 5);
  assert.equal(layout.mainLines.length, 4);
  assert.equal(layout.reworkArcs.length, 2);
  // 两条返工弧线 Y 坐标应错层，避免重叠
  assert.notEqual(layout.reworkArcs[0].labelY, layout.reworkArcs[1].labelY);
  // 节点按 position 严格递增排列
  for (let i = 1; i < layout.ordered.length; i++) {
    assert.ok(layout.ordered[i].x > layout.ordered[i - 1].x);
  }
});
