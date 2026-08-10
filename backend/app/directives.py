"""派发指令常量（零依赖底层，engines 与 harness_adapter 共同引用，避免互相 import）。"""

# 收尾回报约定：回报脚本装在 Bosun 数据目录（report_scripts.py），路径经环境变量
# BOSUN_REPORT_DIR 下发，不再往 ~/.claude 等引擎家目录注入 skill——外部 CLI 的
# 全局环境必须零写入（用户反馈：没装 claude 的机器被凭空建出 ~/.claude）。
# 顺序定为「先回报、后打印正文」：正文若写在 report 调用之前，属于工具调用间的
# 中途文本，各 CLI 都倾向折叠/弱化它，模型也常顺手把正文塞进 --summary 了事；
# 放在 report 之后作为本轮最后一条消息，才稳定以正文形式展示给用户。
REPORT_DIRECTIVE = (
    "\n\n---\n"
    "[Bosun 收尾约定] 本轮工作结束前——无论是任务完成、失败无法继续，"
    "还是需要反问用户才能往下走——都必须收尾，固定两步：①先回报状态：运行 "
    'bash "$BOSUN_REPORT_DIR/report.sh" --status <done|failed|needs_input> '
    '--summary "一句话摘要"，needs_input 时追加 --needs-reply'
    "（Windows 无 bash 时改用 powershell -ExecutionPolicy Bypass -File "
    '"$env:BOSUN_REPORT_DIR\\report.ps1" -Status <状态> -Summary "一句话摘要"，'
    "needs_input 时追加 -NeedsReply）；"
    "②回报之后、停下之前，把本轮完整的结论/分析/待拍板问题正文，作为你最后"
    "一条消息完整打印到终端（回报之后不得再有工具调用）。用户只看这条正文："
    "把正文塞进 --summary 参数、写在工具调用之间、或末尾只补一句短摘要说「见上」，"
    "都等于用户看不到。未回报、或最后一条消息不含完整正文，都不算收尾。"
)
