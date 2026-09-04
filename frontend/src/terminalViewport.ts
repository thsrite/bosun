/** 多端同看一个任务时的终端网格适配。
 *
 * PTY 只有一份 winsize，后端取所有活跃连接的最大值做仲裁（见 backend/app/terminal_viewport.py）。
 * 客户端因此可能被要求按一个比自己容器更大的网格渲染：这时不能改 cols/rows（会与 PTY 排版
 * 错位），而要等比缩小字号，把整幅画面塞进容器——内容完整、排版正确，只是字小。
 *
 * 注意：上报给后端的「期望尺寸」必须始终按**基准字号**换算，不能用缩放后的字号，
 * 否则「字号变小 → 能塞下更多列 → 上报更大 → 仲裁更大 → 字号更小」会自激。
 */

export interface TerminalGrid {
  cols: number;
  rows: number;
}

export interface TerminalCellSize {
  width: number;
  height: number;
}

export interface TerminalBox {
  width: number;
  height: number;
}

/** 字号下限。低于此值实测已不可读（390px 手机塞下 200 列需要约 3px），
 *  与其给一幅看不清的完整画面，不如停在可读字号、让超出的部分横向平移查看。 */
export const MIN_TERMINAL_FONT_SIZE = 8;

/** 容器在基准字号下天然能容纳的网格；与 FitAddon 的下限保持一致（≥2 列 / ≥1 行）。 */
export function naturalGrid(available: TerminalBox, baseCell: TerminalCellSize): TerminalGrid | null {
  if (!(available.width > 0 && available.height > 0)) return null;
  if (!(baseCell.width > 0 && baseCell.height > 0)) return null;
  return {
    cols: Math.max(2, Math.floor(available.width / baseCell.width)),
    rows: Math.max(1, Math.floor(available.height / baseCell.height)),
  };
}

/** 要在容器里完整显示 effective 这个网格所需的字号；装得下就返回基准字号本身。 */
export function fitFontSize(
  baseFontSize: number,
  available: TerminalBox,
  baseCell: TerminalCellSize,
  effective: TerminalGrid,
): number {
  if (!(baseFontSize > 0)) return baseFontSize;
  if (!(available.width > 0 && available.height > 0)) return baseFontSize;
  if (!(baseCell.width > 0 && baseCell.height > 0)) return baseFontSize;
  if (!(effective.cols > 0 && effective.rows > 0)) return baseFontSize;
  const ratio = Math.min(
    available.width / (effective.cols * baseCell.width),
    available.height / (effective.rows * baseCell.height),
  );
  if (ratio >= 1) return baseFontSize;
  // 量化到 0.5px：容器尺寸每抖动一像素都换一次字号会让 xterm 反复重排。
  const scaled = Math.floor(baseFontSize * ratio * 2) / 2;
  return Math.max(MIN_TERMINAL_FONT_SIZE, scaled);
}

/** 解析后端广播的 "\x00meta:size:rows,cols" 控制帧；不是该帧或格式非法时返回 null。 */
export function parseSizeMeta(frame: string): TerminalGrid | null {
  const prefix = "\x00meta:size:";
  if (!frame.startsWith(prefix)) return null;
  const [rows, cols] = frame.slice(prefix.length).split(",");
  const r = Number(rows);
  const c = Number(cols);
  if (!Number.isInteger(r) || !Number.isInteger(c) || r < 1 || c < 1) return null;
  return { rows: r, cols: c };
}
