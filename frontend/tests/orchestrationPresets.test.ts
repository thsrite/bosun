import assert from "node:assert/strict";
import test from "node:test";
import {
  BUILT_IN_ORCHESTRATIONS,
  BUILT_IN_ROLES,
  applyRolePreset,
  nextAvailableOrchestrationName,
} from "../src/orchestrationPresets.ts";

test("built-in roles have unique names and actionable prompts", () => {
  assert.equal(new Set(BUILT_IN_ROLES.map((role) => role.id)).size, BUILT_IN_ROLES.length);
  assert.equal(new Set(BUILT_IN_ROLES.map((role) => role.name)).size, BUILT_IN_ROLES.length);
  for (const role of BUILT_IN_ROLES) {
    assert.ok(role.rolePrompt.length >= 80, `${role.name} prompt should be detailed`);
    assert.match(role.rolePrompt, /输出要求/);
  }
});

test("development orchestration contains four resolvable roles", () => {
  const development = BUILT_IN_ORCHESTRATIONS.find((item) => item.id === "development");
  assert.ok(development);
  assert.equal(development.steps.length, 4);
  for (const step of development.steps) {
    assert.ok(BUILT_IN_ROLES.some((role) => role.id === step.roleId));
  }
});

test("applying a role preset preserves runtime configuration", () => {
  const step = {
    key: "step-1",
    name: "旧名称",
    role_prompt: "旧提示词",
    engine: "codex",
    model: "gpt-5.6-sol",
    reasoning_effort: "high",
  } as const;

  const updated = applyRolePreset(step, "code-review");

  assert.equal(updated.name, "代码审查");
  assert.notEqual(updated.role_prompt, step.role_prompt);
  assert.equal(updated.engine, step.engine);
  assert.equal(updated.model, step.model);
  assert.equal(updated.reasoning_effort, step.reasoning_effort);
  assert.equal(updated.key, step.key);
});

test("built-in orchestration gets a saveable name when copies already exist", () => {
  assert.equal(nextAvailableOrchestrationName("开发", []), "开发");
  assert.equal(nextAvailableOrchestrationName("开发", ["开发"]), "开发 2");
  assert.equal(nextAvailableOrchestrationName("开发", ["开发", "开发 2"]), "开发 3");
});
