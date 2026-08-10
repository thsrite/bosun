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
    "[Bosun 收尾约定] 本轮工作结束前——无论是任务完成、失败无法继续，"
    "还是需要反问用户才能往下走——都必须收尾，固定两步：①先回报状态：向 "
    "$BOSUN_API/api/tasks/$BOSUN_TASK_ID/report POST JSON，字段 result="
    "done|failed|needs_input、summary=≤50字状态回执(不要写结论细节，细节放正文)、"
    "needs_reply=需要用户答复时为 true、reporter_pid=当前 shell 的 pid($$)，"
    "并带请求头 Authorization: Bearer $BOSUN_TASK_TOKEN（该变量为空时省略）。例：\n"
    "curl -sS -X POST -H 'Content-Type: application/json' "
    '-H "Authorization: Bearer $BOSUN_TASK_TOKEN" '
    '-d "{\\"result\\":\\"done\\",\\"summary\\":\\"回执\\",'
    '\\"needs_reply\\":false,\\"reporter_pid\\":$$}" '
    '"$BOSUN_API/api/tasks/$BOSUN_TASK_ID/report"\n'
    "（Windows 用 curl.exe 或 Invoke-RestMethod，reporter_pid 可省略）。"
    "返回非 2xx 说明状态没同步到工作台，须把失败直接告知用户；"
    "②回报之后、停下之前，把本轮完整的结论/分析/待拍板问题正文，作为你最后"
    "一条消息完整打印到终端（回报之后不得再调工具）。用户只看这条正文："
    "把正文塞进 summary 字段、写在工具调用之间、或末尾只补一句短摘要说「见上」，"
    "都等于用户看不到。未回报、或最后一条消息不含完整正文，都不算收尾。"
)

# 催报提醒：回合已结束但后端仍没收到 /report 回调时，作为一条用户消息补投给 agent。
# 必须单行——PTY 路径按整行注入终端输入框，多行会被 TUI 逐行提交。
REPORT_NUDGE = (
    "[Bosun 提醒] 本轮尚未收到收尾回报：请立即按收尾约定向 "
    "$BOSUN_API/api/tasks/$BOSUN_TASK_ID/report 补发 curl 回报"
    "（summary ≤50字回执；正文若已打印不要重复，回报后即可停下）。"
)
