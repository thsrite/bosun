"""派发指令常量（零依赖底层，engines 与 harness_adapter 共同引用，避免互相 import）。"""

# 收尾回报约定：agent 直接 HTTP 回调后端（BOSUN_API / BOSUN_TASK_ID / BOSUN_TASK_TOKEN
# 均已注入任务环境变量），不装 skill、不落脚本——外部 CLI 的全局环境与本机文件系统
# 零注入。reporter_pid 传当前 shell 的 pid（$$），供后端按进程链识别嵌套子 agent 的
# 冒名回报（见 nesting.py，判定在后端，agent 无须自证）。
# 顺序为「先打印正文、再回报」（2026-08-10 翻回）：用户盯终端时结论先出现，curl 只是
# 尾部一行短回执，不再把整段结论塞进命令刷屏。旧顺序治的「漏回报」改由后端催报兜底：
# SdkSession 在回合结束事件上确定性补催，PtySession 静默停在空输入栏时低置信度补催
# （REPORT_NUDGE）；「正文塞进 summary」由 /report 响应的条件式 hint 治。
REPORT_DIRECTIVE = (
    "\n\n---\n"
    "[Bosun 收尾约定] 本轮工作结束前——无论是任务完成、失败无法继续，"
    "还是需要反问用户才能往下走——都必须收尾，固定两步：①先把本轮完整的"
    "结论/分析/待拍板问题正文，作为一条完整消息打印到终端。用户只看这段正文："
    "正文缺失、只给摘要、或让用户去翻工具输出，都等于用户看不到。"
    "②正文打印完，作为本轮最后一个动作回报状态：向 "
    "$BOSUN_API/api/tasks/$BOSUN_TASK_ID/report POST JSON，字段 result="
    "done|failed|needs_input、summary=≤50字状态回执(不要复述正文)、needs_reply="
    "需要用户答复时为 true、reporter_pid=当前 shell 的 pid($$)，并带请求头 "
    "Authorization: Bearer $BOSUN_TASK_TOKEN（该变量为空时省略）。例：\n"
    "curl -sS -X POST -H 'Content-Type: application/json' "
    '-H "Authorization: Bearer $BOSUN_TASK_TOKEN" '
    '-d "{\\"result\\":\\"done\\",\\"summary\\":\\"回执\\",'
    '\\"needs_reply\\":false,\\"reporter_pid\\":$$}" '
    '"$BOSUN_API/api/tasks/$BOSUN_TASK_ID/report"\n'
    "（Windows 用 curl.exe 或 Invoke-RestMethod，reporter_pid 可省略）。"
    "返回非 2xx 说明状态没同步到工作台，须把失败直接告知用户。"
    "未打印完整正文、或未回报，都不算收尾。"
)

# 催报提醒：回合已结束但后端仍没收到 /report 回调时，作为一条用户消息补投给 agent。
# 必须单行——PTY 路径按整行注入终端输入框，多行会被 TUI 逐行提交。
REPORT_NUDGE = (
    "[Bosun 提醒] 本轮尚未收到收尾回报：请立即按收尾约定向 "
    "$BOSUN_API/api/tasks/$BOSUN_TASK_ID/report 补发 curl 回报"
    "（summary ≤50字回执；正文若已打印不要重复，回报后即可停下）。"
)
