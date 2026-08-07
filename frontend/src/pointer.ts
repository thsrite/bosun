/** 触屏（粗指针）设备判定：决定弹窗输入框是否 autoFocus 等移动端专属行为。 */
export function isCoarsePointer(): boolean {
  return (window.matchMedia?.("(pointer: coarse)").matches ?? false) || navigator.maxTouchPoints > 0;
}
