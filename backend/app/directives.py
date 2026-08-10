"""派发指令常量（零依赖底层，engines 与 harness_adapter 共同引用，避免互相 import）。"""

# 收尾回报约定：agent 直接 HTTP 回调后端（BOSUN_API / BOSUN_TASK_ID / BOSUN_TASK_TOKEN
# 均已注入任务环境变量），不装 skill、不落脚本——外部 CLI 的全局环境与本机文件系统
# 零注入。reporter_pid 传当前 shell 的 pid（$$），供后端按进程链识别嵌套子 agent 的
# 冒名回报（见 nesting.py，判定在后端，agent 无须自证）。
# 顺序为「先回报、后打印正文」（2026-08-10 A 方案定稿）：正文放在回报之后、作为本轮
# 最后一条消息，顺着模型「结论放最后」的习惯走，稳定以正文形式展示给用户——曾短暂
# 翻成「先打印后回报」，#524 实测模型会跳过打印步骤、条件式补打提醒又被误判绕过，
# 当天回退。curl 刷屏问题改由 summary 压成 ≤50 字短回执治（结论细节只出现在正文）；
# 彻底漏回报由后端催报兜底（SdkSession 回合结束事件确定性补催，PtySession 静默停在
# 空输入栏时低置信度补催 REPORT_NUDGE）。
REPORT_DIRECTIVE = (
    "\n\n---\n"
    "[Bosun 收尾约定] 每轮结束前（完成、失败、需反问用户都算）固定两步收尾：\n"
    "①回报状态：result=done|failed|needs_input，summary=≤50字回执（细节放正文），"
    "needs_reply=需用户答复时 true。例：\n"
    "curl -sS -X POST -H 'Content-Type: application/json' "
    '-H "Authorization: Bearer $BOSUN_TASK_TOKEN" '
    '-d "{\\"result\\":\\"done\\",\\"summary\\":\\"回执\\",'
    '\\"needs_reply\\":false,\\"reporter_pid\\":$$}" '
    '"$BOSUN_API/api/tasks/$BOSUN_TASK_ID/report"\n'
    "（$BOSUN_TASK_TOKEN 为空可省 Authorization 头；Windows 用 curl.exe，"
    "reporter_pid 可省略；返回非 2xx 须直接告知用户）\n"
    "②回报后不得再调工具，把本轮完整结论正文作为最后一条消息打印到终端。"
    "用户只看这条正文——塞进 summary 或只留「见上」都等于用户看不到。"
    "两步缺一不算收尾。"
)

# 催报提醒：回合已结束但后端仍没收到 /report 回调时，作为一条用户消息补投给 agent。
# 必须单行——PTY 路径按整行注入终端输入框，多行会被 TUI 逐行提交。
REPORT_NUDGE = (
    "[Bosun 提醒] 本轮尚未收到收尾回报：请立即按收尾约定向 "
    "$BOSUN_API/api/tasks/$BOSUN_TASK_ID/report 补发 curl 回报"
    "（summary ≤50字回执；正文若已打印不要重复，回报后即可停下）。"
)
