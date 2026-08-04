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
