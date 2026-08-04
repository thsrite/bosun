# Spec：SDK 任务改对话面板（xterm 只留给 pty）

## 背景 / 问题
cc 首跑走 Claude Agent SDK，输出是**结构化对话内容**（散文回复 + 工具调用 + 结果汇总），
但被灌进 xterm.js 定宽字符网格 → 散文词中间硬折、CJK/emoji 宽度算错光标漂移、容器缩放列数不更新。
xterm 天生不做 word-wrap，补丁只能治标。根治 = SDK 内容用 React 对话面板渲染。

## 原子需求
1. SDK 会话（`use_sdk` 为真的 cc 首跑）在终端区渲染为**对话面板**，非 xterm。
2. 对话面板按消息分块：`text`（Claude 散文，markdown）/ `tool`（工具调用，卡片）/ `result`（本轮 token·成本）/ `error`。
3. CSS word-wrap，中英文/emoji 不再错位、不再词中间断。
4. pty 会话（codex、cc 续聊/压缩、post_input）**保持 xterm 不变**。
5. 实时流 + 断线重连 + 结束后回放 backlog，与现有终端体验对齐。
6. 输入框：用户可输入下一轮指令（等价现在在 xterm 里打字回车）。
7. 权限卡片（现有 getPermission 轮询 + 允许/拒绝）继续可用，不动。

## 验收标准
- 截图里的 `c\nodebase`、单字掉行不再出现（散文按容器宽度优雅换行）。
- cc 首跑任务：文本块渲染为 markdown；工具调用显示为「🔧 工具名 + 入参」卡片；每轮末显示 token·成本。
- codex / cc 续聊任务：终端仍是 xterm，行为零变化。
- 已结束的 SDK 任务重新打开：回放历史对话（从日志解析），不空白、不报错。
- 断线时显示「重连中」，恢复后不重复叠加。

## 设计

### 后端
- **事件格式**：SdkSession 不再 emit ANSI 文本，改 emit **NDJSON**（每行一个 JSON 事件），写日志文件 + 广播 WS。
  - `{"t":"text","text":...}` / `{"t":"tool","name":...,"input":...}` / `{"t":"result","tokens":N,"cost":C}` / `{"t":"error","msg":...}`
  - 日志文件即 NDJSON，`read_backlog` / 结束回放天然复用。
- **渲染模式标记**：task 加列 `render_mode`（'chat' | 'terminal'），`_start_task` 里按 `use_sdk` 落库，`_project_dict`/task detail 返回。前端据此选组件。（`_ensure_columns` 幂等迁移）
- **输入**：SdkSession.write 已支持整行文本作为下一轮 query，ws.py 收 text 直接转发，无需改。
- **权限**：不动（已是独立结构化通道）。

### 前端
- `TerminalPanel` 按 `detail.render_mode` 分流：`'chat'` → `<ChatView>`，否则 `<TerminalView>`（现状）。
- `ChatView`：自建 WS（复用 TerminalView 的重连/回放逻辑骨架），把 NDJSON 累积成消息数组，滚动到底，CSS `break-words`。
  - text → markdown 渲染；tool → 卡片；result → 灰色分隔条；error → 红色。
  - 底部输入框 + 发送，走同一 WS `session.write`。
- 向后兼容：解析不了的行（老 ANSI 日志）降级为纯文本气泡，不报错。

### 待定决策
- **markdown 渲染方式**：react-markdown(+remark-gfm) 依赖 vs 零依赖手写子集 → 见提问。

## 非目标 / 边界
- 不改 pty/xterm 路径。
- 不改权限、配额、调度逻辑。
- 不做语法高亮（代码块用等宽 <pre> 即可，后续可加）。
- 日志下载（get_log）：chat 模式下载 NDJSON 渲染成可读文本（次要，可后置）。

## 分阶段
- **MVP**：后端 NDJSON + render_mode 列；前端 ChatView 基础渲染（text/tool/result/error）+ 输入框 + 重连回放。达成验收标准。
- **v1**：markdown 富渲染（若选依赖）、代码块样式、日志下载文本化。
