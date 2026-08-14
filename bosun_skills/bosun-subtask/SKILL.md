---
name: bosun-subtask
description: 在 Bosun 派发的任务中需要另一个模型给第二意见、交叉复审或独立检查时，优先通过 Bosun 派生受控子任务。仅当 BOSUN_TASK_ID、BOSUN_API、BOSUN_TASK_TOKEN 均存在时自动使用；普通 CLI 会话不要使用。
---

# Bosun 受控子任务

需要第二意见或交叉复审时使用，不要直接启动其他编码 CLI。可用引擎见 `BOSUN_AVAILABLE_ENGINES`。

```sh
curl -sS -X POST -H 'Content-Type: application/json' -H "Authorization: Bearer $BOSUN_TASK_TOKEN" -d "{\"engine\":\"<引擎>\",\"prompt\":\"<任务>\"}" "$BOSUN_API/api/tasks/$BOSUN_TASK_ID/spawn"
```

若返回 `needs_reply:true`，读取 `id` 和 `summary` 后继续回复：

```sh
curl -sS -X POST -H 'Content-Type: application/json' -H "Authorization: Bearer $BOSUN_TASK_TOKEN" -d "{\"message\":\"<回复>\"}" "$BOSUN_API/api/tasks/<子任务id>/reply"
```

可重复回复直到获得最终结论。父任务仍由当前 agent 自己回报；子任务不能再派生子任务。
