#!/usr/bin/env bash
# Bosun 任务状态回调。仅在 Bosun 派发的任务里生效（存在 BOSUN_TASK_ID）。
# 用法: report.sh --status <done|failed|needs_input> --summary "<一句话>" [--needs-reply]
set -euo pipefail

# 守卫：不是 Bosun 派发的会话 → 静默 no-op。
if [ -z "${BOSUN_TASK_ID:-}" ] || [ -z "${BOSUN_API:-}" ]; then
  exit 0
fi

status="done"
summary=""
needs_reply="false"
while [ $# -gt 0 ]; do
  case "$1" in
    --status) status="$2"; shift 2 ;;
    --summary) summary="$2"; shift 2 ;;
    --needs-reply) needs_reply="true"; shift ;;
    *) shift ;;
  esac
done

# 先把同一份汇报留在 agent 终端，再回调 Bosun。
printf 'Bosun 汇报 [%s]: %s\n' "$status" "$summary"

# 用 python3 安全拼 JSON（避免 summary 里的引号/换行破坏报文）。
# reporter_pid 交给后端判断这次回报是不是来自嵌套的子 agent(子 agent 会连
# BOSUN_TASK_ID 一起继承)。判定放后端：它不受 agent 沙箱限制读得到进程表，
# 也知道自己 spawn 的 agent 真实 pid，不必靠进程名去猜谁是顶层。
payload=$(SUMMARY="$summary" STATUS="$status" NEEDS="$needs_reply" PID="$$" python3 -c '
import json, os
print(json.dumps({
    "result": os.environ["STATUS"],
    "summary": os.environ["SUMMARY"],
    "needs_reply": os.environ["NEEDS"] == "true",
    "reporter_pid": int(os.environ["PID"]),
}))')

# 开了访问口令时，回调也要带凭证：BOSUN_TASK_TOKEN 由后端按任务下发，
# 只对本任务的 /report 有效。没开口令时该变量为空，请求照常放行。
# 展开写法 ${a[@]+"${a[@]}"} 是为了兼容 macOS 自带的 bash 3.2：
# 那里 set -u 下直接展开空数组会报 unbound variable。
auth_args=()
if [ -n "${BOSUN_TASK_TOKEN:-}" ]; then
  auth_args=(-H "Authorization: Bearer ${BOSUN_TASK_TOKEN}")
fi

# 回报失败必须看得见：以前一律 `|| true` 吞掉，401/网络不通时终端照样显示
# 「已回报」，Bosun 那头却什么都没收到，没人能发现。
code=$(curl -sS -m 10 -o /dev/null -w '%{http_code}' -X POST \
  -H "Content-Type: application/json" \
  ${auth_args[@]+"${auth_args[@]}"} \
  -d "$payload" \
  "${BOSUN_API}/api/tasks/${BOSUN_TASK_ID}/report" 2>/dev/null) || true
[ -n "$code" ] || code="000"

case "$code" in
  2*)
    # 这行会出现在 agent 的工具结果里：在模型正要停下的时刻提醒它补上正文，
    # 治「结论只写进 --summary、用户要点开命令才看得到」的收尾方式。
    printf '回报已送达。请紧接着把本轮完整结论正文作为你最后一条消息打印出来再停下（不得再调工具）；summary 只是回执，用户只看正文。\n'
    ;;
  *)
    printf 'Bosun 回报失败(HTTP %s)：状态没同步到工作台，请直接告知用户。\n' "$code" >&2
    exit 1
    ;;
esac
