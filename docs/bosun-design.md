# Bosun —— CC/Codex 工作台设计

一个本地 Web 工具：扫描本机多个 git 项目，在每个项目里以**可交互终端会话**形式编排
Claude Code (`claude`) / Codex (`codex`) 跑开发任务，带**并发上限 + 优先级自动调度**，
并提供 **AI 自动诊断流水线（人在环）** 与 **数据统计面板**。

## 技术栈
- 后端：Python / FastAPI + WebSocket + SQLite + `ptyprocess`（pty）
- 前端：React + Vite + Tailwind + dnd-kit（拖拽）+ xterm.js（终端）+ recharts（图表）
- 引擎：`claude` / `codex` CLI，经引擎适配层封装命令差异

## 架构
```
前端  泳道看板 · 拖拽优先级 · xterm 终端 · 建任务 · 问题收件箱 · 统计
  │  REST + WebSocket(终端I/O · 事件推送)
后端
  ├ projects   项目注册(扫描根目录/手动添加)
  ├ tasks      任务CRUD · 优先级 · 状态机
  ├ scheduler  并发上限 · 按优先级挑下一个 · 空槽自动启动
  ├ sessions   每任务一个 pty · 流式I/O · 日志落盘
  ├ engines    cc/codex 命令与 flag 适配
  └ discovery  整体分析 → Finding(测试/构建/lint/审查/git/todo/deps)
```

## 数据模型
- **Project**: id, name, path, created_at
- **Task**: id, project_id, engine(cc|codex), prompt, priority, status, auto_approve,
  session_id, log_path, exit_code, created_at, started_at, ended_at
- **Finding**: id, project_id, source, severity, title, detail, status, task_id, created_at
- **ProjectConfig**: project_id, test_cmd, build_cmd, lint_cmd, enabled_sources, cron
- **Setting**: key, value（如 max_concurrent）

## 状态机
`queued → running → (waiting_input) → done | failed | cancelled`
- 停在 `waiting_input` 的交互会话**仍占并发槽**（面板高亮提醒）
- `waiting_input` 判定：明确确认/选择提示立即进入；静默 > N 秒时还必须看到输入提示形态，避免把 `Working` 动画误判成待输入
- 服务器重启杀掉活 pty → 任务标 `interrupted`，日志保留；`--resume` 续跑放 v1

## 调度语义
全局 `max_concurrent`（默认 3）。有空槽时从 `queued` 中挑 `priority` 最高者启动。
running（含 waiting_input）占槽；done/failed/cancelled 释放槽后自动拉起下一个。

## 自动诊断（人在环）
`触发(手动/cron/事件) → 采集 → 诊断 → Finding清单 → 你勾选 → 生成修复任务 → 调度器跑 → 你review`
- 来源：客观信号(test/build/lint) + cc/codex 审查(结构化JSON) + git/TODO/依赖漏洞
- 人在环：修复任务默认不自动跑，等你批（可勾自动批准做无人值守）

## 默认取舍
1. 后端 Python/FastAPI
2. waiting_input 占槽 + 高亮
3. 权限默认不跳过；每任务可勾"自动批准"(cc `--dangerously-skip-permissions` / codex `--full-auto`)
4. 禁止跨泳道拖拽改归属；拖拽只在项目内改优先级

## 分阶段
- **MVP**：泳道看板 · 建任务 · 并发调度 · pty终端+实时输入 · 日志 · 拖拽优先级 · 手动整体分析→Finding→转修复任务
- **v1**：完整采集 · cron · 重启续跑 · 统计面板基础图表
- **v2**：事件触发(git hook/CI webhook) · 富图表+token成本 · 待输入桌面通知 · 多终端网格
