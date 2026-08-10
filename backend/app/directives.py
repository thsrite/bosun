"""派发指令常量（零依赖底层，engines 与 harness_adapter 共同引用，避免互相 import）。"""

# 收尾回报约定：agent 直接 HTTP 回调后端（BOSUN_API / BOSUN_TASK_ID / BOSUN_TASK_TOKEN
# 均已注入任务环境变量），不装 skill、不落脚本——外部 CLI 的全局环境与本机文件系统
# 零注入。reporter_pid 传当前 shell 的 pid（$$），供后端按进程链识别嵌套子 agent 的
# 冒名回报（见 nesting.py，判定在后端，agent 无须自证）。
# 顺序定为「先回报、后打印正文」：正文若写在回报调用之前，属于工具调用间的
# 中途文本，各 CLI 都倾向折叠/弱化它，模型也常顺手把正文塞进 summary 了事；
# 放在回报之后作为本轮最后一条消息，才稳定以正文形式展示给用户。
REPORT_DIRECTIVE = (
    "\n\n---\n"
    "[Bosun 收尾约定] 本轮工作结束前——无论是任务完成、失败无法继续，"
    "还是需要反问用户才能往下走——都必须收尾，固定两步：①先回报状态：向 "
    "$BOSUN_API/api/tasks/$BOSUN_TASK_ID/report POST JSON，字段 result="
    "done|failed|needs_input、summary=一句话摘要(≤200字)、needs_reply="
    "需要用户答复时为 true、reporter_pid=当前 shell 的 pid($$)，并带请求头 "
    "Authorization: Bearer $BOSUN_TASK_TOKEN（该变量为空时省略）。例：\n"
    "curl -sS -X POST -H 'Content-Type: application/json' "
    '-H "Authorization: Bearer $BOSUN_TASK_TOKEN" '
    '-d "{\\"result\\":\\"done\\",\\"summary\\":\\"摘要\\",'
    '\\"needs_reply\\":false,\\"reporter_pid\\":$$}" '
    '"$BOSUN_API/api/tasks/$BOSUN_TASK_ID/report"\n'
    "（Windows 用 curl.exe 或 Invoke-RestMethod，reporter_pid 可省略）。"
    "返回非 2xx 说明状态没同步到工作台，须把失败直接告知用户；"
    "②回报之后、停下之前，把本轮完整的结论/分析/待拍板问题正文，作为你最后"
    "一条消息完整打印到终端（回报之后不得再有工具调用）。用户只看这条正文："
    "把正文塞进 summary 字段、写在工具调用之间、或末尾只补一句短摘要说「见上」，"
    "都等于用户看不到。未回报、或最后一条消息不含完整正文，都不算收尾。"
)
