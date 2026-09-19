---
name: bosun-subtask
description: 在 Bosun 派发的任务中需要另一个模型给第二意见、交叉复审或独立检查时，优先通过 Bosun 派生受控子任务，并负责监控、接收结果和确认回收。仅当 BOSUN_TASK_ID、BOSUN_API、BOSUN_TASK_TOKEN 均存在时自动使用；普通 CLI 会话不要使用。
---

# Bosun 受控子任务

需要第二意见或交叉复审时使用，不要直接启动其他编码 CLI。可用引擎见 `BOSUN_AVAILABLE_ENGINES`。派发成功不是协作完成：父任务必须跟进到结果已读取、子任务已回收。

## 派发与监控同时建立

先在 prompt 中写清范围、验收条件、禁止修改的内容，以及完整结果的交付位置。复审等长结果应指定父任务可读的独立结果文件，要求子任务先保存结果、再立即回报，不能只在自己的终端输出后等待。

```sh
curl --fail-with-body -sS --connect-timeout 10 --max-time 3700 -X POST -H 'Content-Type: application/json' -H "Authorization: Bearer $BOSUN_TASK_TOKEN" -d '{"engine":"<引擎>","prompt":"<任务与结果交付位置>","timeout":900}' "$BOSUN_API/api/tasks/$BOSUN_TASK_ID/spawn"
```

- `/spawn` 是同步等待接口，不是只返回任务编号的创建接口。派发后服务端立即每 0.5 秒检查回报，完成、失败或需要答复时返回；默认等待 900 秒，超时会终止子任务。
- 工具把请求转为后台执行时，**同一轮建立跟进机制**：保留后台任务句柄并启用完成通知；没有完成通知能力时，建立每 30 秒检查一次该后台任务状态的定时监控。不能使用无人跟进的 `curl ... &`，也不能派发后结束父任务。
- 父任务可以继续独立工作，但完成通知一到就应读取并处理结果，不等用户追问。只在没有可做的工作时阻塞等待，不用高频轮询制造空转。
- 监控必须有终止条件：最终结果已处理、请求失败或服务端超时后停止监控；不得永久留下计时器、轮询进程或后台请求。
- 非 2xx 或连接中断不是“子任务已完成”。已有子任务编号时通过下面的结果接口补查；不要因为没收到响应而盲目重复派发同一任务。

## 读取结果与继续问答

读取响应的 `id`、`status`、`result`、`summary`、`needs_reply`、`timed_out`，并读取约定的结果文件；不要把简短回执当作完整结论。

若返回 `needs_reply:true`，保留子任务会话，及时回答其问题；回复请求会重新进入服务端定时监控：

```sh
curl --fail-with-body -sS --connect-timeout 10 --max-time 3700 -X POST -H 'Content-Type: application/json' -H "Authorization: Bearer $BOSUN_TASK_TOKEN" -d '{"message":"<回复>","timeout":900}' "$BOSUN_API/api/tasks/<子任务id>/reply"
```

已知编号时可补查状态与回报；需要持续补查时使用每 30 秒一次的有界监控，并在最终结果出现或达到本轮超时后停止：

```sh
curl --fail-with-body -sS --connect-timeout 10 --max-time 30 -H "Authorization: Bearer $BOSUN_TASK_TOKEN" "$BOSUN_API/api/tasks/<子任务id>/result"
```

`finished:false` 或 `needs_reply:true` 不能当作最终完成。需要父任务决策时先处理问题，不能直接关闭子任务。

## 读取最终结果后立即确认回收

服务端在同步请求拿到最终结果时会自动结束子任务会话。父任务读完结果后仍须调用幂等确认接口，确保补查等路径也完成回收，并停止自己建立的监控：

```sh
curl --fail-with-body -sS --connect-timeout 10 --max-time 60 -X POST -H "Authorization: Bearer $BOSUN_TASK_TOKEN" "$BOSUN_API/api/tasks/<子任务id>/result/ack"
```

- 只确认最终结果；`needs_input`、仍运行或等待输入但没有最终回报的任务会被拒绝，不能用取消接口绕过。已由人工或进程退出进入终态的任务也可确认回收，但仍带 `needs_input` 的任务除外。
- 成功响应含 `acknowledged:true` 和最终 `status`。已经回收的任务可以重复确认；失败状态不会改成成功。
- “关闭”只结束运行会话，保留任务记录、日志与回报；不得删除结果。
- 回收确认失败时必须告知用户，不得声称子任务已关闭。父任务处理完所有子任务结果和监控资源后，再回报自己的最终状态。

父任务仍由当前 agent 自己回报；子任务不能再派生子任务。
