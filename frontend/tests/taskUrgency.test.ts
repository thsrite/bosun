import assert from "node:assert/strict";
import test from "node:test";
import {
  groupForSwitcher,
  matchesQuery,
  nextAttentionTask,
  sortByUrgency,
  urgencyGroup,
} from "../src/taskUrgency.ts";

const task = (id: number, status: string, extra: Record<string, unknown> = {}) => ({ id, status, ...extra });

test("waiting tasks that need the user come first, finished-awaiting-review last", () => {
  const tasks = [
    task(1, "running"),
    task(2, "waiting_input", { waiting_kind: "review" }),
    task(3, "queued"),
    task(4, "waiting_input", { waiting_kind: "choice", waiting_since: 200 }),
    task(5, "waiting_input", { waiting_kind: "input", waiting_since: 100 }),
    task(6, "done"),
  ];
  assert.deepEqual(sortByUrgency(tasks).map((t) => t.id), [5, 4, 1, 3, 2, 6]);
});

test("permission prompts count as needing the user even when kind is review", () => {
  assert.equal(urgencyGroup(task(1, "waiting_input", { waiting_kind: "review", pending_perm: true })), "attention");
  assert.equal(urgencyGroup(task(2, "waiting_input", { waiting_kind: null })), "attention");
  assert.equal(
    urgencyGroup(task(3, "waiting_input", { waiting_kind: "review", report_result: "needs_input" })),
    "attention",
  );
  assert.equal(urgencyGroup(task(4, "waiting_input", { waiting_kind: "review", report_result: "done" })), "review");
});

test("equal urgency keeps the caller's order", () => {
  const tasks = [task(9, "running"), task(3, "running"), task(7, "running")];
  assert.deepEqual(sortByUrgency(tasks).map((t) => t.id), [9, 3, 7]);
});

test("next attention task skips the current one", () => {
  const tasks = [
    task(1, "waiting_input", { waiting_since: 10 }),
    task(2, "waiting_input", { waiting_since: 20 }),
    task(3, "running"),
  ];
  assert.equal(nextAttentionTask(tasks, 1)?.id, 2);
  assert.equal(nextAttentionTask(tasks, 3)?.id, 1);
  assert.equal(nextAttentionTask([task(3, "running")], 1), null);
});

test("search matches id, title, project and summary with all words required", () => {
  const t = task(42, "running", { title: "修复登录", engine: "codex", report_summary: "等待确认" });
  assert.ok(matchesQuery(t, "#42"));
  assert.ok(matchesQuery(t, "登录 codex"));
  assert.ok(matchesQuery(t, "grading", "grading_system"));
  assert.ok(matchesQuery(t, "确认"));
  assert.ok(!matchesQuery(t, "登录 claude"));
});

test("switcher groups drop empty groups and follow urgency order", () => {
  const groups = groupForSwitcher(
    [task(1, "running", { title: "a" }), task(2, "waiting_input", { title: "b" }), task(3, "queued", { title: "a" })],
    "a",
  );
  assert.deepEqual(groups.map((g) => [g.group, g.tasks.map((t) => t.id)]), [
    ["running", [1]],
    ["queued", [3]],
  ]);
});
