import { api } from "./api";
import { confirmDialog } from "./overlay";

/** 执行前限额守卫：目标引擎任一用量窗口超阈值则弹确认。返回是否继续。engine 省略=查全部。 */
export async function guardQuota(engine?: string): Promise<boolean> {
  // omp/kimi 自带 provider 凭据，Browser 走 API Key；都没有 CLI 订阅额度接口。
  if (engine === "omp" || engine === "kimi" || engine === "browser") return true;
  try {
    const q = await api.quota(engine);
    if (q.enabled === false) return true;
    const limit = q.block_pct ?? 90;
    const list = engine
      ? [{ name: engine === "cc" ? "Claude" : "Codex", ...(engine === "cc" ? q.claude : q.codex) }]
      : [
          { name: "Claude", ...q.claude },
          { name: "Codex", ...q.codex },
        ];
    const over = list.flatMap((p: any) => {
      if (!p?.available) return [];
      return [
        ["5 小时", p.five_hour_pct],
        ["周", p.weekly_pct],
      ]
        .filter(([, pct]) => typeof pct === "number" && pct >= limit)
        .map(([window, pct]) => `${p.name} ${window}用量 ${pct}%`);
    });
    if (over.length) {
      const msg = over.join("、");
      return confirmDialog(`⚠️ ${msg}（≥${limit}%），继续可能烧爆配额。仍要执行吗？`, { danger: true });
    }
  } catch {
    /* 用量拿不到就放行 */
  }
  return true;
}
