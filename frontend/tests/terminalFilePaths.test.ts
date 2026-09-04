import assert from "node:assert/strict";
import { test } from "node:test";

import { extractPathAt } from "../src/terminalFilePaths.ts";

const at = (line: string, marker: string) => {
  const index = line.indexOf(marker);
  assert.ok(index >= 0, `marker ${marker} not in line`);
  return index;
};

test("从普通输出里取出相对路径", () => {
  const line = "  已写入 docs/design/notes.md 共 42 行";
  assert.equal(extractPathAt(line, at(line, "docs/")), "docs/design/notes.md");
});

test("从任意一列点进去都取到同一个路径", () => {
  const line = "wrote src/components/App.tsx";
  const start = at(line, "src/");
  for (let i = start; i < start + "src/components/App.tsx".length; i += 1) {
    assert.equal(extractPathAt(line, i), "src/components/App.tsx");
  }
});

test("绝对路径", () => {
  const line = "saved to /Users/me/proj/out/shot.png";
  assert.equal(extractPathAt(line, at(line, "/Users")), "/Users/me/proj/out/shot.png");
});

test("剥掉编译器输出的 :行:列 后缀", () => {
  const line = "src/app.ts:120:8 - error TS2345";
  assert.equal(extractPathAt(line, at(line, "src/")), "src/app.ts");
});

test("剥掉只有行号的后缀", () => {
  const line = "at frontend/src/api.ts:42";
  assert.equal(extractPathAt(line, at(line, "frontend/")), "frontend/src/api.ts");
});

test("剥掉句末标点和包裹的括号", () => {
  const line = "见 (report/summary.md).";
  assert.equal(extractPathAt(line, at(line, "report/")), "report/summary.md");
});

test("引号里的路径可以带空格", () => {
  const line = 'open "my docs/年度 报告.pdf" now';
  assert.equal(extractPathAt(line, at(line, "my docs")), "my docs/年度 报告.pdf");
});

test("单引号同样处理", () => {
  const line = "cat 'a b/c d.txt'";
  assert.equal(extractPathAt(line, at(line, "a b/")), "a b/c d.txt");
});

test("点击引号之外不会误吞整段", () => {
  const line = 'open "docs/a.md" and src/b.ts';
  assert.equal(extractPathAt(line, at(line, "src/b")), "src/b.ts");
});

test("~ 开头的路径保留原样交给后端解析", () => {
  const line = "config at ~/.config/app.toml";
  assert.equal(extractPathAt(line, at(line, "~/")), "~/.config/app.toml");
});

test("没有扩展名也没有斜杠的普通词不当作路径", () => {
  const line = "compiling successfully";
  assert.equal(extractPathAt(line, at(line, "compiling")), null);
});

test("空白处点击返回 null", () => {
  const line = "a.md   b.md";
  assert.equal(extractPathAt(line, 5), null);
});

test("越界列返回 null", () => {
  assert.equal(extractPathAt("a.md", -1), null);
  assert.equal(extractPathAt("a.md", 99), null);
});

test("URL 不当作本地文件路径", () => {
  const line = "see https://example.com/a.png";
  assert.equal(extractPathAt(line, at(line, "https")), null);
});

test("TUI 框线不会被吞进路径", () => {
  const line = "│ wrote out/a.png │";
  assert.equal(extractPathAt(line, at(line, "out/")), "out/a.png");
});

test("行尾被截断的路径也按可见部分返回", () => {
  const line = "reading src/very/deep/file.json";
  assert.equal(extractPathAt(line, line.length - 1), "src/very/deep/file.json");
});

test("无扩展名但带斜杠的路径仍然接受", () => {
  const line = "run scripts/build";
  assert.equal(extractPathAt(line, at(line, "scripts/")), "scripts/build");
});
