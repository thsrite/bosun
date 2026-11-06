/** 原始日志中的光标列号属于写入时的终端，不能直接在手机窄屏上回放。
 * 先为绝对定位留够列，再由 xterm 在回放完成后按当前宽度重排。
 * 不扩大行数：行数影响滚动区域，扩大高度会改变历史滚动的语义。
 */
export function terminalReplayColumns(data: string | Uint8Array, currentCols: number): number {
  const text = typeof data === "string" ? data : new TextDecoder().decode(data);
  let cols = currentCols;
  const positions = /\x1b\[(?:(\d*);(\d*)[Hf]|(\d+)[G`])/g;
  for (const match of text.matchAll(positions)) {
    const column = Number(match[2] || match[3] || 1);
    // 列号是 1-based；再留一格，避免双宽汉字在最后一列强制换行。
    cols = Math.max(cols, Math.min(512, column + 1));
  }
  return cols;
}

/** 备用屏幕由应用自行重绘，不能用普通历史缓冲区的重排/补换行去改它。 */
export function hasAlternateScreenSequence(data: string | Uint8Array): boolean {
  const text = typeof data === "string" ? data : new TextDecoder().decode(data);
  return /\x1b\[\?(?:\d+;)*(?:47|1047|1049)(?:;\d+)*h/.test(text);
}

/** 把回放的最后一行移入历史，并将光标放到新的空行。
 * xterm 缩窄时不会重排光标所在行；光标停在屏幕上方时还可能裁掉底部重排的内容。
 * 用独立空行承接光标后再缩窄，完整正文都能参与重排。实时画面由重连后的重绘接续。
 */
export function prepareTerminalReplay(data: string | Uint8Array, rows: number): string | Uint8Array {
  const suffix = `\x1b[r\x1b[${rows};1H\r\n`;
  if (typeof data === "string") return data + suffix;
  const ending = new TextEncoder().encode(suffix);
  const result = new Uint8Array(data.byteLength + ending.byteLength);
  result.set(data);
  result.set(ending, data.byteLength);
  return result;
}
