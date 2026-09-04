/** 「谁在操作，PTY 就归谁」——多端同看一个任务时的视口认领。
 *
 * 一个任务只有一个 PTY、一份 winsize，多端做不到各自一个尺寸。之前的做法是任一端
 * 只在自己容器尺寸变化或刚连上时上报一次，于是谁最后上报谁说了算：手机端一连上就把
 * PTY 压到手机宽度，电脑端的窗口尺寸没变、ResizeObserver 不触发，就再也抢不回来，
 * 全屏 TUI 一直挤在左边窄条里，除非刷新页面或拖一下窗口。
 *
 * 改为在「本端正在被操作」时重新认领 PTY：有键盘输入、窗口获得焦点、页面回到前台。
 * 语义可预期——你在哪台设备上动手，就按那台的宽度排版；两端都静止时谁也不动，
 * 不会互相抢。PTY 侧对同值 resize 做了短路（pty_session.resize），认领是幂等的。
 */

export interface TerminalGrid {
  rows: number;
  cols: number;
}

/** 尺寸没变时两次认领的最小间隔。连续打字每个按键都发控制帧没有意义，
 *  但间隔太长又会让「敲一下抢回来」变得迟钝；1s 下第一个按键即时生效。 */
export const CLAIM_THROTTLE_MS = 1000;

export interface ClaimState {
  /** 上次认领时上报的网格 */
  grid: TerminalGrid;
  /** 上次认领的时间戳（performance.now / Date.now 同源即可） */
  at: number;
}

/** 此刻是否应该把本端的网格重新写进 PTY。
 *
 * 尺寸变了必须立即上报——那是本端自己的排版需求，不能被节流压掉；
 * 尺寸没变则是在跟另一端抢 PTY，按节流窗口放行。
 */
export function shouldClaimViewport(
  last: ClaimState | null,
  current: TerminalGrid,
  now: number,
  throttleMs: number = CLAIM_THROTTLE_MS,
): boolean {
  if (!(current.rows > 0 && current.cols > 0)) return false;
  if (last === null) return true;
  if (last.grid.rows !== current.rows || last.grid.cols !== current.cols) return true;
  return now - last.at >= throttleMs;
}
