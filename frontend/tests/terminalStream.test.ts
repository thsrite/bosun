import assert from "node:assert/strict";
import { test } from "node:test";

import {
  MAX_DEFER_MS,
  shouldDeferTerminalWrite,
  shouldResumeDeferredWrites,
} from "../src/terminalStream.ts";

const base = {
  touchDevice: true,
  touchActive: false,
  hasSelection: false,
  overflowed: false,
  deferredSince: null as number | null,
  now: 1000,
};

test("手指按在屏上时先攒着不写", () => {
  assert.equal(shouldDeferTerminalWrite({ ...base, touchActive: true }), true);
});

test("存在选区时先攒着不写", () => {
  assert.equal(shouldDeferTerminalWrite({ ...base, hasSelection: true }), true);
});

test("桌面端从不暂停", () => {
  assert.equal(
    shouldDeferTerminalWrite({ ...base, touchDevice: false, touchActive: true }),
    false,
  );
});

test("上滑看历史不再扣住输出——脱离底部只关自动跟随", () => {
  // 手指已松开、没有选区：无论视口停在哪里都照常写入
  assert.equal(shouldDeferTerminalWrite({ ...base }), false);
});

test("闸门已放行后一路直写", () => {
  assert.equal(
    shouldDeferTerminalWrite({ ...base, touchActive: true, overflowed: true }),
    false,
  );
});

test("按住不放超过上限即放行", () => {
  const state = { ...base, touchActive: true, deferredSince: 1000 };
  assert.equal(shouldDeferTerminalWrite({ ...state, now: 1000 + MAX_DEFER_MS - 1 }), true);
  assert.equal(shouldDeferTerminalWrite({ ...state, now: 1000 + MAX_DEFER_MS }), false);
});

test("暂停原因消失才补写", () => {
  assert.equal(shouldResumeDeferredWrites({ touchActive: false, hasSelection: false }), true);
  assert.equal(shouldResumeDeferredWrites({ touchActive: true, hasSelection: false }), false);
  assert.equal(shouldResumeDeferredWrites({ touchActive: false, hasSelection: true }), false);
});
