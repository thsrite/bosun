/** 发光状态点：绿/黄/红/中性，带柔和光晕。 */
export function GlowDot({ tone, size = 10 }: { tone: "ok" | "warn" | "crit" | "idle"; size?: number }) {
  const color =
    tone === "ok" ? "#34d399" : tone === "warn" ? "#fbbf24" : tone === "crit" ? "#f87171" : "#cbd5e1";
  return (
    <span
      style={{
        width: size,
        height: size,
        background: color,
        boxShadow: tone === "idle" ? "none" : `0 0 8px ${color}66`,
      }}
      className="inline-block rounded-full"
    />
  );
}

/** 趋势徽标：▲ 升(绿) / ▼ 降(红) / ─ 平。dir 由数值符号决定。 */
export function TrendBadge({ value, suffix = "" }: { value: number; suffix?: string }) {
  if (value === 0) return <span className="text-[11px] text-slate-400">─</span>;
  const up = value > 0;
  return (
    <span className={`text-[11px] font-medium ${up ? "text-emerald-500" : "text-rose-500"}`}>
      {up ? "▲" : "▼"} {Math.abs(value)}
      {suffix}
    </span>
  );
}
