import type { WorkflowEdge, WorkflowGraph, WorkflowNode } from "./types.ts";

const NODE_W = 224;
const NODE_H = 126;
const GAP_X = 76;
const PAD_X = 30;
const PAD_TOP = 64;
const PAD_BOTTOM = 44;

export interface NodeBox {
  node: WorkflowNode;
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface LaneBox {
  id: string;
  title: string;
  x: number;
  w: number;
}

export interface ComputedLayout {
  width: number;
  height: number;
  nodes: Record<string, NodeBox>;
  ordered: NodeBox[];
  lanes: LaneBox[];
  reworkArcs: { edge: WorkflowEdge; path: string; labelX: number; labelY: number }[];
  mainLines: { edge: WorkflowEdge; path: string; midX: number; midY: number }[];
}

export function computeWorkflowLayout(graph: WorkflowGraph): ComputedLayout {
  const orderedNodes = [...graph.nodes].sort((a, b) => a.position - b.position);
  const nodes: Record<string, NodeBox> = {};
  const ordered: NodeBox[] = [];

  let cursorX = PAD_X;
  let prevLane: string | null = null;
  for (const node of orderedNodes) {
    if (prevLane && prevLane !== node.lane) {
      cursorX += 20;
    }
    const box: NodeBox = { node, x: cursorX, y: PAD_TOP, w: NODE_W, h: NODE_H };
    nodes[node.id] = box;
    ordered.push(box);
    cursorX += NODE_W + GAP_X;
    prevLane = node.lane;
  }

  const totalWidth = Math.max(680, cursorX - GAP_X + PAD_X);
  const lanes: LaneBox[] = [];
  for (const lane of graph.lanes) {
    const inLane = ordered.filter((b) => b.node.lane === lane.id);
    if (!inLane.length) continue;
    const minX = inLane[0].x - 12;
    const maxX = inLane[inLane.length - 1].x + NODE_W + 12;
    lanes.push({ id: lane.id, title: lane.title, x: minX, w: maxX - minX });
  }

  const mainLines: ComputedLayout["mainLines"] = [];
  const reworkArcs: ComputedLayout["reworkArcs"] = [];
  let reworkSlot = 0;
  let askSlot = 0;

  for (const edge of graph.edges) {
    const src = nodes[edge.source];
    const tgt = nodes[edge.target];
    if (!src || !tgt) continue;

    if (edge.kind === "handoff" && tgt.x >= src.x) {
      const x1 = src.x + src.w;
      const y1 = src.y + src.h / 2;
      const x2 = tgt.x;
      const y2 = tgt.y + tgt.h / 2;
      const c = Math.max(20, (x2 - x1) * 0.45);
      const path = `M ${x1} ${y1} C ${x1 + c} ${y1}, ${x2 - c} ${y2}, ${x2} ${y2}`;
      mainLines.push({ edge, path, midX: (x1 + x2) / 2, midY: (y1 + y2) / 2 - 12 });
    } else if (edge.kind === "rework") {
      const lift = 28 + reworkSlot * 26;
      reworkSlot += 1;
      const x1 = src.x + src.w * 0.5;
      const y1 = src.y;
      const x2 = tgt.x + tgt.w * 0.5;
      const y2 = tgt.y;
      const cy = Math.max(10, PAD_TOP - lift);
      const path = `M ${x1} ${y1} C ${x1} ${cy}, ${x2} ${cy}, ${x2} ${y2}`;
      reworkArcs.push({ edge, path, labelX: (x1 + x2) / 2, labelY: cy + 8 });
    } else {
      const drop = 26 + askSlot * 24;
      askSlot += 1;
      const x1 = src.x + src.w * 0.5;
      const y1 = src.y + src.h;
      const x2 = tgt.x + tgt.w * 0.5;
      const y2 = tgt.y + tgt.h;
      const cy = PAD_TOP + NODE_H + drop;
      const path = `M ${x1} ${y1} C ${x1} ${cy}, ${x2} ${cy}, ${x2} ${y2}`;
      reworkArcs.push({ edge, path, labelX: (x1 + x2) / 2, labelY: cy - 4 });
    }
  }

  const extraBottom = askSlot > 0 ? askSlot * 24 + 14 : 0;
  const totalHeight = PAD_TOP + NODE_H + PAD_BOTTOM + extraBottom;
  return { width: totalWidth, height: totalHeight, nodes, ordered, lanes, reworkArcs, mainLines };
}
