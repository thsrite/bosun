import { strict as assert } from "node:assert";
import { test } from "node:test";
import { CLAIM_THROTTLE_MS, shouldClaimViewport } from "../src/terminalClaim.ts";

const grid = (rows: number, cols: number) => ({ rows, cols });

test("首次认领无条件放行", () => {
  assert.equal(shouldClaimViewport(null, grid(30, 100), 0), true);
});

test("尺寸变了立即放行，不受节流压制", () => {
  const last = { grid: grid(30, 100), at: 1000 };
  // 距上次认领仅 1ms，远小于节流窗口
  assert.equal(shouldClaimViewport(last, grid(30, 80), 1001), true);
  assert.equal(shouldClaimViewport(last, grid(24, 100), 1001), true);
});

test("尺寸没变且在节流窗口内不重发", () => {
  const last = { grid: grid(30, 100), at: 1000 };
  assert.equal(shouldClaimViewport(last, grid(30, 100), 1000 + CLAIM_THROTTLE_MS - 1), false);
});

test("尺寸没变但已过节流窗口则放行——这是从另一端抢回 PTY 的路径", () => {
  const last = { grid: grid(30, 100), at: 1000 };
  assert.equal(shouldClaimViewport(last, grid(30, 100), 1000 + CLAIM_THROTTLE_MS), true);
});

test("节流窗口可配置", () => {
  const last = { grid: grid(30, 100), at: 0 };
  assert.equal(shouldClaimViewport(last, grid(30, 100), 50, 100), false);
  assert.equal(shouldClaimViewport(last, grid(30, 100), 100, 100), true);
});

test("非法网格不认领——xterm 首帧未就绪时 rows/cols 可能为 0", () => {
  assert.equal(shouldClaimViewport(null, grid(0, 100), 0), false);
  assert.equal(shouldClaimViewport(null, grid(30, 0), 0), false);
});
