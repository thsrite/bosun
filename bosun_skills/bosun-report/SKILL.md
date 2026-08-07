---
name: bosun-report
description: 向 Bosun 工作台回报当前任务的最终状态。仅当你正运行在 Bosun 派发的任务中（环境变量 BOSUN_TASK_ID 存在）时适用；在任务收尾、你即将停下等待用户、或任务失败无法继续时调用，让 Bosun 拿到权威状态而不必靠猜。若不在 Bosun 任务中（无 BOSUN_TASK_ID），本 skill 无操作，不要调用。
---

# bosun-report

把当前任务的最终状态回报给 Bosun 工作台，取代它对终端输出的启发式猜测。

## 何时使用

- **仅当** 环境变量 `BOSUN_TASK_ID` 存在（说明本会话由 Bosun 派发）。
- 在你这一轮工作**收尾时**：任务完成、失败、或需要用户拍板才能继续。
- 不适用：环境里没有 `BOSUN_TASK_ID`（你是被用户单独开启的）→ 什么都不做。
- 不适用：**你是被另一个 agent 拉起来的子 agent**（比如别人让你跑一轮代码审查、做一次
  子任务）。`BOSUN_TASK_ID` 是从上游 agent 的环境继承来的，那个任务的状态该由派发它的
  agent 回报，不是你。后端也会按进程链识别并忽略这类回报，但你不该主动调。

## 怎么做

**回报前必须先把完整结论打印到终端**：用户只看你的终端输出，分析/结论/交付物正文要作为
你收尾前的最后一条消息完整打出来，不能只说「见上」，更不能把正文只塞进 `--summary` 参数里。
`summary` 是给工作台看的一句话摘要，替代不了正文。

正文打印完后，运行本 skill 目录下的 `scripts/report.sh`，二选一传状态：

- 任务已完成：
  ```bash
  bash "$(dirname "$0")/scripts/report.sh" --status done --summary "一句话说清你做了什么"
  ```
- 任务失败/无法继续：
  ```bash
  bash scripts/report.sh --status failed --summary "一句话说清卡在哪"
  ```
- 需要用户输入才能继续：
  ```bash
  bash scripts/report.sh --status needs_input --summary "一句话说清等用户拍板什么" --needs-reply
  ```

`summary` 用一句话（≤200 字）说清结论。脚本会先把状态和同一份 summary 打印到 agent 终端，再回调 Bosun；同时会自动处理引号转义，直接写自然语言即可。
脚本在非 Bosun 会话里会自动空转，安全无副作用。

**脚本非零退出并打印「Bosun 回报失败(HTTP …)」时**：状态没同步到工作台（工作台那边看不到这条任务在等你），
不要当作已回报，把这条失败连同 HTTP 码一并告诉用户。开了访问口令的实例靠环境变量
`BOSUN_TASK_TOKEN` 鉴权，它由后端在派发任务时注入，你不需要自己设置。
