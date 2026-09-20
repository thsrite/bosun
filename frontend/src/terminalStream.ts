/** 移动端终端「暂停写入」的判定——什么时候可以扣住 PTY 输出、什么时候必须放行。
 *
 * 触摸端逐帧手势滚动时，新输出一边写一边重排，画面会被扯着跑；系统/xterm 选区也会被
 * 写入冲掉。所以手势期间把输出先攒进队列、松手再补写。
 *
 * 但「用户上滑看历史」不属于这一类：那是一个可以持续很久的阅读状态，扣住输出就成了
 * 「一上滑任务就不动了」。xterm 在视口脱离底部时写入并不会移动视口，照常写入不影响
 * 阅读；脱离底部要关掉的只是自动跟随（scrollToBottom），不是写入本身。因此这里只认
 * 两个短暂条件：手指正按在屏上、存在选区。
 *
 * 另加两道兜底闸门：攒够 MAX_DEFERRED_TERMINAL_BYTES 字节、或暂停超过 MAX_DEFER_MS，
 * 无条件放行——长按不松手、选区一直留着，都不该让任务看起来停更。
 */

/** 暂停期间最多攒这么多字节，超出即放行 */
export const MAX_DEFERRED_TERMINAL_BYTES = 4 * 1024 * 1024;
/** 暂停最长持续这么久，超时即放行 */
export const MAX_DEFER_MS = 1500;

export interface TerminalPauseState {
  /** 是否触摸设备：桌面端不需要暂停，写入与滚轮不冲突 */
  touchDevice: boolean;
  /** 手指正按在终端上（touchstart 起、touchend 止） */
  touchActive: boolean;
  /** 终端里存在选区，写入会把它冲掉 */
  hasSelection: boolean;
  /** 闸门已放行（字节超限或超时），本轮暂停不再生效 */
  overflowed: boolean;
  /** 本轮暂停开始的时间戳，未在暂停则为 null */
  deferredSince: number | null;
  now: number;
}

/** 此刻的输出应该先攒进队列，而不是直接写进 xterm。 */
export function shouldDeferTerminalWrite(state: TerminalPauseState): boolean {
  if (!state.touchDevice) return false;
  if (state.overflowed) return false;
  if (state.deferredSince !== null && state.now - state.deferredSince >= MAX_DEFER_MS) return false;
  return state.touchActive || state.hasSelection;
}

/** 暂停的原因已经消失，可以把攒下的输出补写出去。 */
export function shouldResumeDeferredWrites(
  state: Pick<TerminalPauseState, "touchActive" | "hasSelection">,
): boolean {
  return !state.touchActive && !state.hasSelection;
}
