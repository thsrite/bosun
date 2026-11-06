import assert from "node:assert/strict";
import { test } from "node:test";
import headless from "@xterm/headless";
import { createTerminalResizeScheduler } from "../src/terminalResize.ts";

test("旧屏幕底部的定位输出先写完，再切换到键盘弹出后的行数", async () => {
  const term = new headless.Terminal({ cols: 60, rows: 40, allowProposedApi: true });
  try {
    await new Promise<void>((resolve) => {
      const scheduler = createTerminalResizeScheduler(
        (done) => {
          // 模拟尺寸稳定时，旧尺寸下生成的多行提示仍在异步写入队列。
          term.write("\x1b[38;1H输入提示\x1b[39;1H模型信息\x1b[40;1H快捷键");
          term.write("", done);
        },
        () => {
          assert.equal(term.buffer.active.getLine(37)!.translateToString(true), "输入提示");
          assert.equal(term.buffer.active.getLine(38)!.translateToString(true), "模型信息");
          assert.equal(term.buffer.active.getLine(39)!.translateToString(true), "快捷键");
          term.resize(60, 15);
          assert.equal(term.rows, 15);
          scheduler.dispose();
          resolve();
        },
        0,
      );
      scheduler.schedule();
    });
  } finally {
    term.dispose();
  }
});

test("键盘连续改变高度时只提交最终尺寸，并等待旧输出完成", (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const drains: (() => void)[] = [];
  let height = 40;
  const applied: number[] = [];
  const scheduler = createTerminalResizeScheduler((done) => drains.push(done), () => applied.push(height), 180);
  for (height = 40; height > 15; height--) {
    scheduler.schedule();
    t.mock.timers.tick(16);
    assert.equal(scheduler.pending, true);
  }
  assert.deepEqual(applied, []);
  assert.equal(drains.length, 0);
  t.mock.timers.tick(180);
  assert.equal(scheduler.pending, true);
  assert.deepEqual(applied, []);
  drains.shift()!();
  assert.deepEqual(applied, [15]);
  assert.equal(scheduler.pending, false);
  scheduler.dispose();
});

test("等待输出期间的新尺寸取代旧回调，卸载后不再重排", (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const drains: (() => void)[] = [];
  let applied = 0;
  const scheduler = createTerminalResizeScheduler((done) => drains.push(done), () => applied++, 180);
  scheduler.schedule();
  t.mock.timers.tick(180);
  scheduler.schedule();
  drains.shift()!();
  assert.equal(applied, 0);
  assert.equal(scheduler.pending, true);
  t.mock.timers.tick(180);
  scheduler.dispose();
  drains.shift()!();
  assert.equal(applied, 0);
  scheduler.schedule();
  scheduler.dispose();
  t.mock.timers.tick(180);
  assert.equal(drains.length, 0);
});
