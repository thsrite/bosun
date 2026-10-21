/**
 * 工作台格子拖动：把任务 fromId 放到第 toSlot 格，返回新的任务顺序（顺序即格子位置）。
 * 落在有任务的格子 → 两者互换；落在空格 → 移到末尾（格子按顺序填充，末尾就是第一个空格）。
 * 无需变动时返回 null。
 */
export function moveToSlot(taskIds: number[], fromId: number, toSlot: number): number[] | null {
  const from = taskIds.indexOf(fromId);
  if (from < 0 || from === toSlot || toSlot < 0) return null;
  const next = [...taskIds];
  if (toSlot < next.length) {
    [next[from], next[toSlot]] = [next[toSlot], next[from]];
  } else {
    if (from === next.length - 1) return null;
    next.splice(from, 1);
    next.push(fromId);
  }
  return next;
}
