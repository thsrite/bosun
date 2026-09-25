import assert from "node:assert/strict";
import { test } from "node:test";
import { formatTerminalSelectionLines } from "../src/terminalSelection.ts";

function line(text: string, endColumn: number, wrapped = false) {
  return { text, fullText: text, endColumn, wrapped };
}

test("中文列表的排版续行合并，标题和空行保留", () => {
  const rows = [
    line(" 📖  文档站", 12), line("", 0),
    line("  - 教程新增私有群组与搜索机器人、媒体库快捷入口、入库通", 56),
    line("    知模板变量、上传中断恢复、订阅扫描与转存顺序等使用说", 56),
    line("    明", 6),
  ];
  assert.equal(formatTerminalSelectionLines(rows, 64),
    " 📖  文档站\n\n  - 教程新增私有群组与搜索机器人、媒体库快捷入口、入库通知模板变量、上传中断恢复、订阅扫描与转存顺序等使用说明");
});

test("英文续行保留词间空格", () => {
  assert.equal(formatTerminalSelectionLines([
    line("  - Documentation includes private groups and search", 51),
    line("    bots and media library shortcuts", 35),
  ], 56), "  - Documentation includes private groups and search bots and media library shortcuts");
});

test("独立列表项、短行和嵌套列表不合并", () => {
  const texts = ["  - 这是抵达右边缘的一条完整列表内容", "  - 这是下一条列表内容", "    下一行是主动换行", "    - 嵌套列表"];
  assert.equal(formatTerminalSelectionLines(texts.map((text, i) => line(text, i === 0 ? 60 : 30)), 64), texts.join("\n"));
});

test("代码块、表格和命令保留换行", () => {
  for (const texts of [
    ["```text", "这一行是代码块里的较长中文说明", "下一行仍然属于代码内容", "```"],
    ["const message = '这一行内容抵达右边缘';", "下一行不能合并进代码"],
    ["| 第一列是很长的中文内容 | 第二列 |", "| 下一行 | 仍然是表格 |"],
    ["$ echo 这是一个较长的命令参数", "$ echo 下一条命令"],
  ]) {
    assert.equal(formatTerminalSelectionLines(texts.map((text) => line(text, 60)), 64), texts.join("\n"));
  }
});

test("真实软换行不添加空格或删除续行空格", () => {
  assert.equal(formatTerminalSelectionLines([
    line("https://example.com/very/long/path", 32),
    line("/with-more", 10, true),
    line("next paragraph", 14),
  ], 32), "https://example.com/very/long/path/with-more\nnext paragraph");
});

test("缺少列表上下文的缩进行保留换行，不扩展选区", () => {
  const first = line("    从正文中间选择直到右边缘", 60);
  first.text = "正文中间选择直到右边缘";
  assert.equal(formatTerminalSelectionLines([first, line("    剩余内容", 12)], 64),
    "正文中间选择直到右边缘\n    剩余内容");
});
