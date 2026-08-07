import type { Engine } from "./types";

/** 引擎展示元数据。新增引擎只改这里，避免各处散落 `cc ? A : B` 的二值兜底。 */
const ENGINE_META: Record<Engine, { name: string; short: string; badge: string }> = {
  cc: { name: "Claude Code", short: "Claude", badge: "bg-violet-500/15 text-violet-300" },
  codex: { name: "Codex", short: "Codex", badge: "bg-emerald-500/10 text-emerald-300" },
  omp: { name: "Oh My Pi", short: "omp", badge: "bg-sky-500/10 text-sky-300" },
  kimi: { name: "Kimi Code", short: "Kimi", badge: "bg-amber-500/10 text-amber-300" },
};

/** 界面里引擎的展示顺序。 */
export const ENGINE_ORDER: Engine[] = ["cc", "codex", "omp", "kimi"];

/** 各引擎的全权限运行参数，用于提示语。 */
export const AUTO_APPROVE_FLAG: Record<Engine, string> = {
  cc: "--dangerously-skip-permissions",
  codex: "--dangerously-bypass-approvals-and-sandbox",
  omp: "--auto-approve",
  kimi: "--yolo",
};

const FALLBACK = { name: "未知引擎", short: "?", badge: "bg-dh-s2 text-dh-muted" };

function meta(engine: string) {
  return ENGINE_META[engine as Engine] ?? { ...FALLBACK, name: engine || FALLBACK.name };
}

/** 完整名称，用于提示语与标题。 */
export function engineName(engine: string): string {
  return meta(engine).name;
}

/** 短标签，用于徽标与按钮。 */
export function engineShort(engine: string): string {
  return meta(engine).short;
}

/** 徽标配色。 */
export function engineBadgeClass(engine: string): string {
  return meta(engine).badge;
}
