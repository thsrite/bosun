"""派发指令常量（零依赖底层，engines 与 harness_adapter 共同引用，避免互相 import）。"""

# 收尾回报约定的降级文本：正常路径由 bosun-report skill 提供，只有 skill 安装失败或
# 同名冲突时，回合结束后的催报才把本段投给 agent。reporter_pid 传当前 shell 的 pid
#（$$），供后端按进程链识别嵌套子 agent 的冒名回报。
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

# 编排步骤先用纯文本端点提交完整阶段产物，再发短 JSON 回报。纯文本上传避免多行、引号
# 和 Markdown 在 shell/JSON 双层转义中损坏。它追加在当前引擎 harness 的受保护收尾约定
# 之后，既不改变普通任务，也不会被可演进 directive 覆盖。
ORCHESTRATION_REPORT_ADDENDUM = (
    "\n当前任务是编排步骤，上面的收尾改为三步且顺序以这里为准：\n"
    "①先提交完整阶段产物（把占位内容替换为正文）：\n"
    "curl -sS -X POST -H 'Content-Type: text/plain; charset=utf-8' "
    "-H \"Authorization: Bearer $BOSUN_TASK_TOKEN\" --data-binary @- "
    "\"$BOSUN_API/api/tasks/$BOSUN_TASK_ID/artifact\" <<'BOSUN_ARTIFACT'\n"
    "<完整阶段产物>\n"
    "BOSUN_ARTIFACT\n"
    "②再执行上面的 JSON 短回报；③最后打印完整结论正文，回报后不得再调工具。"
    "artifact 不是摘要，必须足以让下一个角色脱离本终端日志继续工作；"
    "done 缺少 artifact 会被后端拒绝。Windows 可用 curl.exe 配合 UTF-8 临时文件"
    "和 `--data-binary @文件路径` 提交。\n"
    "【班组协作】编排里全体角色同时在线，接力棒只有一根：\n"
    "· 没轮到你时保持待命，不要开工、不要回报 done（会被后端按接力棒守卫拒绝）；"
    "Bosun 会把交棒、返工意见和别人的提问直接投进本会话，收到再动。\n"
    "· 发现前面某位做错了：把 result 改成 rework，加 target_position=<第几位>，"
    "summary 写返工意见（只能打回给你前面的角色，全程返工次数有上限，超限转人工）。\n"
    "· 想问班组里另一位（不改变接力棒）：\n"
    "curl -sS -X POST -H 'Content-Type: application/json' "
    '-H "Authorization: Bearer $BOSUN_TASK_TOKEN" '
    '-d "{\\"to_position\\":2,\\"body\\":\\"你的问题\\",\\"reporter_pid\\":$$}" '
    '"$BOSUN_API/api/tasks/$BOSUN_TASK_ID/message"\n'
    "· 最后一位是汇报角色，由它输出面向用户的最终结论；其余角色不要越位替它总结。"
)
