/** 键盘动画结束后再改变终端网格；等待已有输出消费完，避免旧坐标写入新网格。 */
export function createTerminalResizeScheduler(
  drain: (done: () => void) => void,
  resize: () => void,
  delayMs: number,
) {
  let timer: ReturnType<typeof setTimeout> | undefined;
  let generation = 0;
  let pending = false;
  return {
    get pending() { return pending; },
    schedule() {
      pending = true;
      const current = ++generation;
      clearTimeout(timer);
      timer = setTimeout(() => {
        timer = undefined;
        drain(() => {
          if (current !== generation) return;
          pending = false;
          resize();
        });
      }, delayMs);
    },
    dispose() {
      ++generation;
      clearTimeout(timer);
      pending = false;
    },
  };
}
