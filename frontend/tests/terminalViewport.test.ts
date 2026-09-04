import assert from "node:assert/strict";
import { test } from "node:test";

import {
  MIN_TERMINAL_FONT_SIZE,
  fitFontSize,
  naturalGrid,
  parseSizeMeta,
} from "../src/terminalViewport.ts";

const CELL = { width: 7, height: 14 };

test("naturalGrid 按基准字号的字元尺寸换算容器网格", () => {
  assert.deepEqual(naturalGrid({ width: 700, height: 280 }, CELL), { cols: 100, rows: 20 });
});

test("naturalGrid 向下取整，不产生半个字元", () => {
  assert.deepEqual(naturalGrid({ width: 703, height: 289 }, CELL), { cols: 100, rows: 20 });
});

test("naturalGrid 对退化容器返回 null", () => {
  assert.equal(naturalGrid({ width: 0, height: 280 }, CELL), null);
  assert.equal(naturalGrid({ width: 700, height: 0 }, CELL), null);
  assert.equal(naturalGrid({ width: 700, height: 280 }, { width: 0, height: 14 }), null);
});

test("装得下仲裁网格时保持基准字号", () => {
  const size = fitFontSize(12, { width: 700, height: 280 }, CELL, { cols: 100, rows: 20 });
  assert.equal(size, 12);
});

test("容器比仲裁网格窄时按宽度比缩小字号", () => {
  // 容器 525px 宽，被要求渲染 100 列（基准下需要 700px）→ 0.75 倍
  const size = fitFontSize(12, { width: 525, height: 280 }, CELL, { cols: 100, rows: 20 });
  assert.equal(size, 9);
});

test("高度不够时按高度比缩小，取两轴中更严格的那个", () => {
  // 宽度装得下（比 1），高度只有 0.75 → 取高度这一轴
  const size = fitFontSize(12, { width: 700, height: 210 }, CELL, { cols: 100, rows: 20 });
  assert.equal(size, 9);
});

test("字号量化到 0.5px，避免容器抖动引发反复重排", () => {
  const size = fitFontSize(12, { width: 690, height: 280 }, CELL, { cols: 100, rows: 20 });
  assert.equal(size, 11.5);
});

test("再小也不缩到不可读以下——超出部分留给横向平移", () => {
  const size = fitFontSize(12, { width: 40, height: 280 }, CELL, { cols: 100, rows: 20 });
  assert.equal(size, MIN_TERMINAL_FONT_SIZE);
  assert.ok(size > 0);
});

test("手机端塞桌面端网格时停在可读下限，而不是缩到看不清", () => {
  // 390px 手机 vs 164 列桌面网格：等比需要约 3px，实测不可读，停在下限
  const size = fitFontSize(12, { width: 390, height: 300 }, CELL, { cols: 164, rows: 21 });
  assert.equal(size, MIN_TERMINAL_FONT_SIZE);
});

test("退化输入回退到基准字号而不是 NaN", () => {
  assert.equal(fitFontSize(12, { width: 0, height: 0 }, CELL, { cols: 100, rows: 20 }), 12);
  assert.equal(fitFontSize(12, { width: 700, height: 280 }, CELL, { cols: 0, rows: 20 }), 12);
});

test("parseSizeMeta 解析尺寸广播帧", () => {
  assert.deepEqual(parseSizeMeta("\x00meta:size:40,200"), { rows: 40, cols: 200 });
});

test("parseSizeMeta 拒绝其它 meta 帧与畸形值", () => {
  assert.equal(parseSizeMeta("\x00meta:backlog_truncated"), null);
  assert.equal(parseSizeMeta("\x00meta:size:40"), null);
  assert.equal(parseSizeMeta("\x00meta:size:abc,200"), null);
  assert.equal(parseSizeMeta("\x00meta:size:0,200"), null);
  assert.equal(parseSizeMeta("\x00meta:size:1.5,200"), null);
});
