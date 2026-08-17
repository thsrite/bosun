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

## 编排班组（只在编排任务里适用）

编排里全体角色同时在线，接力棒只有一根。开场 prompt 会写明你是第几位、当前谁持棒。

- **没轮到你**：保持待命，不要开工、不要回报 `done`（后端会按接力棒守卫拒绝）。交棒、返工意见、别人的提问都会由 Bosun 直接投进本会话，收到再动。
- **收到带“可靠投递 / 消息 #N”的消息**：处理正文前先原样执行消息末尾的 ACK 命令。
  ACK 只确认消息已经进入本回合，不会改变接力棒，也不代表任务完成；非 2xx 必须说明。
- **打回返工**：`result` 用 `rework`，加 `target_position`（打回给第几位，只能是你前面的角色），`summary` 写返工意见。全程返工次数有上限，超限编排转人工裁决。

```sh
curl -sS -X POST -H 'Content-Type: application/json' -H "Authorization: Bearer $BOSUN_TASK_TOKEN" -d "{\"result\":\"rework\",\"summary\":\"方案缺回滚路径，请补充\",\"target_position\":1,\"reporter_pid\":$$}" "$BOSUN_API/api/tasks/$BOSUN_TASK_ID/report"
```

- **问班组里的另一位**（不改变接力棒，对方在自己的会话里作答）：

```sh
curl -sS -X POST -H 'Content-Type: application/json' -H "Authorization: Bearer $BOSUN_TASK_TOKEN" -d "{\"to_position\":2,\"body\":\"这个接口你打算怎么实现？\",\"reporter_pid\":$$}" "$BOSUN_API/api/tasks/$BOSUN_TASK_ID/message"
```

单条消息上限 4000 字，整轮编排消息数与同一对角色的连续往返都有上限，触顶会熔断转人工——有话直说，别对着踢皮球。

- **最后一位是汇报角色**：由它通读全部产物与消息，输出面向用户的最终结论。其余角色不要越位替它总结。
