import { createContext, useContext } from "react";
import { ENGINE_ORDER } from "./engines";
import type { Engine } from "./types";

/** 各引擎是否已安装；null = 还没探测出结果。 */
export type InstalledEngines = Record<string, boolean> | null;

export const InstalledEnginesContext = createContext<InstalledEngines>(null);

/** 原始探测结果，需要区分「还没探测」与「没装」时用。 */
export function useInstalledEngines(): InstalledEngines {
  return useContext(InstalledEnginesContext);
}

/**
 * 该引擎的相关信息是否要展示。
 * 探测结果未回来时按已安装处理：常见情况是装齐的，先隐藏再显示会闪一下。
 */
export function useEngineVisible(engine: string): boolean {
  const installed = useInstalledEngines();
  return installed ? installed[engine] !== false : true;
}

/** 可展示、可选择的引擎列表，同样在探测出结果前按已安装处理。 */
export function useAvailableEngines(): Engine[] {
  const installed = useInstalledEngines();
  return ENGINE_ORDER.filter((engine) => !installed || installed[engine] !== false);
}
