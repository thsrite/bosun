import assert from "node:assert/strict";
import { test } from "node:test";
import headless from "@xterm/headless";
import { hasAlternateScreenSequence, prepareTerminalReplay, terminalReplayColumns } from "../src/terminalReplay.ts";

test("备用屏幕重绘只保留当前画面，并恢复应用接管滚轮的模式", async () => {
  const term = new headless.Terminal({ cols: 50, rows: 10, allowProposedApi: true });
  const prefix = "\x1b[?1049h\x1b[?1003h\x1b[?1006h";
  assert.equal(hasAlternateScreenSequence(new TextEncoder().encode(prefix)), true);
  assert.equal(hasAlternateScreenSequence("\x1b[?25;1049h"), true);
  assert.equal(hasAlternateScreenSequence("\x1b[?1049l"), false);
  await new Promise<void>((resolve) => term.write(prefix + "\x1b[2J\x1b[1;1H旧画面\x1b[9;1HAsk Codex", resolve));
  for (let i = 0; i < 3; i++) {
    await new Promise<void>((resolve) => term.write("\x1b[2J\x1b[1;1H更早的历史\x1b[9;1HAsk Codex", resolve));
  }
  assert.equal(term.buffer.active.type, "alternate");
  assert.equal(term.modes.mouseTrackingMode, "any");
  const text = Array.from({ length: term.buffer.active.length }, (_, i) => term.buffer.active.getLine(i)!.translateToString(true)).join("\n");
  assert.equal(text.match(/Ask Codex/g)?.length, 1);
  assert.ok(text.includes("更早的历史"));
  assert.ok(!text.includes("旧画面"));
  term.dispose();
});

test("普通文本不改变回放宽度，绝对定位给双宽字符留足空间", () => {
  assert.equal(terminalReplayColumns("普通日志\r\n", 60), 60);
  assert.equal(terminalReplayColumns("\x1b[20;120H中\x1b[122G文", 60), 123);
  assert.equal(terminalReplayColumns(new TextEncoder().encode("\x1b[2;150f字"), 60), 151);
  assert.equal(terminalReplayColumns("\x1b[999999;999999H", 60), 512);
});

test("桌面逐字定位日志在手机回放不会制造逐字竖排，后续输出仍按手机宽度显示", async () => {
  const message = "移动端查看终端时应该正常换行并保留完整的中文内容避免逐字挤到屏幕左侧";
  const payload = [...message].map((char, i) => `\x1b[6;${i * 2 + 1}H${char}`).join("") + "\x1b[1;1H";
  const render = async (prepare: boolean) => {
    const term = new headless.Terminal({ cols: 24, rows: 6, scrollback: 1000, allowProposedApi: true });
    if (prepare) term.resize(terminalReplayColumns(payload, term.cols), term.rows);
    await new Promise<void>((resolve) => term.write(prepare ? prepareTerminalReplay(payload, term.rows) : payload, resolve));
    if (prepare) term.resize(24, 6);
    const lines = Array.from({ length: term.buffer.active.length }, (_, i) =>
      term.buffer.active.getLine(i)!.translateToString(true));
    assert.equal(term.cols, 24);
    await new Promise<void>((resolve) => term.write("\x1b[1;1H后续输出", resolve));
    assert.match(term.buffer.active.getLine(term.buffer.active.baseY)!.translateToString(true), /后续输出/);
    term.dispose();
    return lines;
  };
  const broken = await render(false);
  const fixed = await render(true);
  assert.ok(broken.filter((line) => /^[\u4e00-\u9fff]$/.test(line.trim())).length > 5);
  assert.ok(fixed.filter((line) => /^[\u4e00-\u9fff]$/.test(line.trim())).length <= 1);
  assert.ok(fixed.join("").replace(/\s/g, "").includes(message));
});
