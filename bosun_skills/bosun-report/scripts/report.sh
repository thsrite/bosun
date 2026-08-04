#!/usr/bin/env bash
# Bosun 任务状态回调。仅在 Bosun 派发的任务里生效（存在 BOSUN_TASK_ID）。
# 用法: report.sh --status <done|failed|needs_input> --summary "<一句话>" [--needs-reply]
set -euo pipefail

# 守卫：不是 Bosun 派发的会话 → 静默 no-op。
if [ -z "${BOSUN_TASK_ID:-}" ] || [ -z "${BOSUN_API:-}" ]; then
  exit 0
fi

# 守卫：BOSUN_TASK_ID 会随环境继承给 agent 自己拉起的子 agent(如让 codex 跑一轮
# 审查)，子 agent 装了同一个 skill 就会拿父任务的 id 乱报状态。按父进程链形状识别，
# 判不准一律放行。详见同目录 nesting.py。
if [ -n "${BOSUN_BACKEND_PID:-}" ] && command -v python3 >/dev/null 2>&1; then
  _guard_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  if [ -f "$_guard_dir/nesting.py" ]; then
    _nested="$(python3 "$_guard_dir/nesting.py" "$$" "$BOSUN_BACKEND_PID" 2>/dev/null || echo ok)"
    if [ "$_nested" = "nested" ]; then
      printf '跳过 Bosun 回报：当前是嵌套 agent，任务 #%s 的状态应由派发它的 agent 回报。\n' \
        "$BOSUN_TASK_ID" >&2
      exit 0
    fi
  fi
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
payload=$(SUMMARY="$summary" STATUS="$status" NEEDS="$needs_reply" python3 -c '
import json, os
print(json.dumps({
    "result": os.environ["STATUS"],
    "summary": os.environ["SUMMARY"],
    "needs_reply": os.environ["NEEDS"] == "true",
}))')

curl -sS -m 10 -X POST \
  -H "Content-Type: application/json" \
  -d "$payload" \
  "${BOSUN_API}/api/tasks/${BOSUN_TASK_ID}/report" >/dev/null || true
