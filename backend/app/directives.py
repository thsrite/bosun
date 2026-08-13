"""派发指令常量（零依赖底层，engines 与 harness_adapter 共同引用，避免互相 import）。"""

# 收尾回报约定：agent 直接 HTTP 回调后端（BOSUN_API / BOSUN_TASK_ID / BOSUN_TASK_TOKEN
# 均已注入任务环境变量），不装 skill、不落脚本——外部 CLI 的全局环境与本机文件系统
# 零注入。reporter_pid 传当前 shell 的 pid（$$），供后端按进程链识别嵌套子 agent 的
# 冒名回报（见 nesting.py，判定在后端，agent 无须自证）。
# Authorization 头要求逐字照抄：曾写成「token 为空可省」，agent 据此改写成
# `${BOSUN_TASK_TOKEN:+-H "Authorization: Bearer $BOSUN_TASK_TOKEN"}`，而 zsh 对未加引号
# 的参数展开不分词，整段塌成单个参数 `-H Authorization: Bearer <tok>`，curl 发出的请求头
# 名带前导空格，被 uvicorn/h11 在进 FastAPI 前判为非法（回 `Invalid HTTP request
# received.`，curl 退出码仍是 0），回报静默丢失。token 由 auth.issue_task_token 每轮必发，
# 本就不会为空，那句提示纯属自找麻烦。
# 顺序为「先回报、后打印正文」（2026-08-10 A 方案定稿）：正文放在回报之后、作为本轮
# 最后一条消息，顺着模型「结论放最后」的习惯走，稳定以正文形式展示给用户——曾短暂
# 翻成「先打印后回报」，#524 实测模型会跳过打印步骤、条件式补打提醒又被误判绕过，
# 当天回退。curl 刷屏问题改由 summary 压成 ≤50 字短回执治（结论细节只出现在正文）；
# 回报本身写成「机械动作、照抄模板、不要分析」并允许「完成/失败/待答复」极简回执，
# 压缩收尾时花在组装命令和措辞 summary 上的推理停顿；
# 彻底漏回报由后端催报兜底（SdkSession 回合结束事件确定性补催，PtySession 静默停在
# 空输入栏时低置信度补催 REPORT_NUDGE）。
REPORT_DIRECTIVE = (
    "\n\n---\n"
    "[Bosun 收尾约定] 每轮结束前（完成/失败/需反问都算）固定两步收尾：\n"
    "①回报——机械照抄此命令填好字段即发，不要分析："
    "result=done|failed|needs_input，summary=≤50字回执（写不出就用"
    "「完成/失败/待答复」），needs_reply=需用户答复时 true。例：\n"
    "curl -sS -X POST -H 'Content-Type: application/json' "
    '-H "Authorization: Bearer $BOSUN_TASK_TOKEN" '
    '-d "{\\"result\\":\\"done\\",\\"summary\\":\\"回执\\",'
    '\\"needs_reply\\":false,\\"reporter_pid\\":$$}" '
    '"$BOSUN_API/api/tasks/$BOSUN_TASK_ID/report"\n'
    "（Authorization 头照抄，别做 ${VAR:+…} 这类条件拼接——zsh 不分词，"
    "会拼成一个畸形参数，请求进不了后端；Windows 用 curl.exe，"
    "reporter_pid 可省；非 2xx 须告知用户）\n"
    "②回报后不得再调工具，把本轮完整结论正文作为最后一条消息打印到终端——"
    "用户只看这条正文，塞进 summary 或只留「见上」都等于没说。"
    "两步缺一不算收尾。"
)

# 引擎清单提示：告诉 agent 同机还装了哪些别的编码 CLI，可自行调用。
# 刻意只列引擎、不列技能——cc 等 CLI 自带 skill 发现（实测本机 230 个），再注入
# 一份是纯冗余。措辞刻意保守：默认不调，只在确有第二意见/交叉复审价值时才调，
# 免得 agent 把简单任务也甩给子进程，白烧另一份额度。
# 子 agent 会继承 BOSUN_TASK_ID 并可能冒名回报，后端有 nesting.py 按进程链拦截，
# 这里再明确提醒一句「别让它回报」，双保险。
ENGINE_ROSTER_TEMPLATE = (
    "\n\n---\n"
    "[Bosun 环境] 本机还装了这些编码 CLI，可在终端直接调用：{engines}。\n"
    "默认不用；仅当确有价值时才调（例如要另一个模型出第二意见、交叉复审你自己的改动）。"
    "调用它们属于你这一轮工作的一部分，**收尾回报仍由你自己发**，"
    "不要让子 agent 代为回报（它会冒用本任务身份）。"
)

# 子任务派生提示：开启受控子任务后取代 ENGINE_ROSTER_TEMPLATE 的后半段。
# 刻意做成「一条 curl 拿回结论」的同步形态——agent 现在一条 `codex exec` 就能同步
# 拿到复审结论，换成异步+轮询比直接跑 CLI 更麻烦，它会绕开不用。写法与收尾回报
# 约定保持一致（机械照抄、不要分析），压缩组装命令时的推理停顿。
SUBTASK_TEMPLATE = (
    "\n\n---\n"
    "[Bosun 环境] 需要另一个模型出第二意见/交叉复审时，**优先用下面这条派生子任务**，"
    "不要自己在终端里直接起别的 CLI——直接起的那个 Bosun 看不见、管不了、也不计额度。"
    "可用引擎：{engines}。命令会阻塞到子任务本轮回报并把内容打印出来：\n"
    "curl -sS -X POST -H 'Content-Type: application/json' "
    '-H "Authorization: Bearer $BOSUN_TASK_TOKEN" '
    '-d "{{\\"engine\\":\\"<引擎>\\",\\"prompt\\":\\"<要它做什么>\\"}}" '
    '"$BOSUN_API/api/tasks/$BOSUN_TASK_ID/spawn"\n'
    "若返回 `needs_reply:true`，读取其中的 `id` 和 `summary`，决定后用父任务同一凭证回复：\n"
    "curl -sS -X POST -H 'Content-Type: application/json' "
    '-H "Authorization: Bearer $BOSUN_TASK_TOKEN" '
    '-d "{{\\"message\\":\\"<回复内容>\\"}}" '
    '"$BOSUN_API/api/tasks/<子任务id>/reply"\n'
    "可重复回复，直到返回最终结论。"
    "（默认最多 3 个子任务、单个最长 15 分钟；子任务不能再派生子任务。"
    "**本轮的收尾回报仍由你自己发**，子任务的回报只属于它自己。）"
)

# 催报提醒：回合已结束但后端仍没收到 /report 回调时，作为一条用户消息补投给 agent。
# 必须单行——PTY 路径按整行注入终端输入框，多行会被 TUI 逐行提交。
REPORT_NUDGE = (
    "[Bosun 提醒] 本轮尚未收到收尾回报：请立即按收尾约定向 "
    "$BOSUN_API/api/tasks/$BOSUN_TASK_ID/report 补发 curl 回报"
    "（summary ≤50字回执；正文若已打印不要重复，回报后即可停下）。"
)
