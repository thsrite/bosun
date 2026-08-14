---
name: bosun-report
description: 在 Bosun 派发的任务中结束当前工作回合时，向 Bosun 回报 done、failed 或 needs_input。仅当 BOSUN_TASK_ID、BOSUN_API、BOSUN_TASK_TOKEN 均存在时自动使用；普通 CLI 会话不要使用。
---

# Bosun 任务回报

仅在三个 `BOSUN_*` 环境变量都存在时执行。完成、失败、需要用户答复都必须回报。

若 `BOSUN_ARTIFACT_REQUIRED=1`，先把完整阶段产物提交到 artifact 端点：

```sh
curl -sS -X POST -H 'Content-Type: text/plain; charset=utf-8' -H "Authorization: Bearer $BOSUN_TASK_TOKEN" --data-binary @- "$BOSUN_API/api/tasks/$BOSUN_TASK_ID/artifact" <<'BOSUN_ARTIFACT'
<完整阶段产物>
BOSUN_ARTIFACT
```

再回报本轮状态。`summary` 不超过 50 字；需要用户答复时使用 `needs_input` 和 `needs_reply:true`：

```sh
curl -sS -X POST -H 'Content-Type: application/json' -H "Authorization: Bearer $BOSUN_TASK_TOKEN" -d "{\"result\":\"done\",\"summary\":\"回执\",\"needs_reply\":false,\"reporter_pid\":$$}" "$BOSUN_API/api/tasks/$BOSUN_TASK_ID/report"
```

Authorization 头必须原样保留。非 2xx 要告知用户。回报成功后不得再调用工具，把本轮完整结论正文作为最后一条消息输出；summary 不能代替正文。Windows 使用 `curl.exe`，可省略 `reporter_pid`。
