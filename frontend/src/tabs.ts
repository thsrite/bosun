/** 顶栏一级导航；App 与设置页（默认打开页面）共用，避免两处各写一份。 */
export type Tab = "projects" | "tasks" | "claude" | "stats" | "delivery" | "settings";

export const NAV: { key: Tab; label: string }[] = [
  { key: "projects", label: "项目" },
  { key: "tasks", label: "运行任务" },
  { key: "claude", label: "Claude 管理" },
  { key: "stats", label: "统计图表" },
  { key: "delivery", label: "交付监控" },
  { key: "settings", label: "设置" },
];
