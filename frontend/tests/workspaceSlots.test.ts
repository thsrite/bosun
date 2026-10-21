import assert from "node:assert/strict";
import test from "node:test";
import { moveToSlot } from "../src/workspaceSlots.ts";

test("dropping onto an occupied slot swaps the two tasks", () => {
  assert.deepEqual(moveToSlot([1, 2, 3], 1, 2), [3, 2, 1]);
  assert.deepEqual(moveToSlot([1, 2, 3], 3, 0), [3, 2, 1]);
});

test("dropping onto an empty slot moves the task to the first empty position", () => {
  assert.deepEqual(moveToSlot([1, 2, 3], 1, 4), [2, 3, 1]);
});

test("no-op drops return null", () => {
  assert.equal(moveToSlot([1, 2, 3], 2, 1), null);
  assert.equal(moveToSlot([1, 2, 3], 3, 5), null);
  assert.equal(moveToSlot([1, 2, 3], 9, 0), null);
});
