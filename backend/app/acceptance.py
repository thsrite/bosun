"""三角色交付的验收协议。结构化记录与可读说明同存在阶段产物中。"""

import json
import re
import subprocess

BLOCK = re.compile(
    r"^\x60{3}bosun-acceptance[ \t]*\r?\n(.*?)^\x60{3}[ \t]*\r?$", re.M | re.S
)
GUIDES = (
    "需求交棒前在阶段产物中附一个 bosun-acceptance 围栏 JSON。例："
    '{"delivery":"review","items":[{"id":"A1","input":"测试账号与样本","expected":"可观察结果"}]}。'
    "真实任务替换例值。delivery=review 交付待审结果，任务明确要求合并才填 merge 并写 base 为目标分支。所有列出的项都是必验项。",
    "开发交棒前在阶段产物中附一个 bosun-acceptance 围栏 JSON。例："
    '{"version":"候选提交完整 SHA","self_test":"实际执行与结果","evidence":"记录位置"}。'
    "非代码任务使用可定位的文档版本或产物指纹。返修后更新为本轮版本。",
    "质量复验时以需求原始必验项为准。在阶段产物中附一个 bosun-acceptance 围栏 JSON。例："
    '{"version":"本次实际测试的版本","items":[{"id":"A1","status":"通过","actual":"观察到的结果","evidence":"测试记录位置"}],'
    '"review":{"status":"通过","actual":"代码评审结论","evidence":"评审记录"}}。'
    "状态取通过、失败或未测；未测写明原因。已知失败退开发，未测等待补验。完成回报时系统也会按这些状态分流。"
    "若交付目标是 merge，还需提供 merge 对象，包含 ci、approval、authorization 三个同格式的判定记录，分别对应当前候选的必要 CI、适用的产品签收、当次合并授权。ci 额外写 version 对应检查的提交。"
    "merge.pr 填本项目的 GitHub PR 编号（正整数）。先保存产物，再 GET $BOSUN_API/api/tasks/$BOSUN_TASK_ID/acceptance-check?version=实际候选版本，使用自己的任务 Bearer token。"
    "仅返回 passed=true 后才可尝试对同一提交合并；保留仓库保护，使用匹配 head 提交的合并方式。合并后 POST 同一地址，由 Bosun 向 GitHub 读取合并回执，然后再回报 done。",
)


def record(artifact):
    blocks = BLOCK.findall(artifact or "")
    if len(blocks) != 1:
        raise ValueError("阶段产物需要一个以三反引号 bosun-acceptance 开始的 JSON 区块")
    data = json.loads(blocks[0])
    if not isinstance(data, dict):
        raise ValueError("验收记录应为 JSON 对象")
    return data


def required(row, key):
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"验收字段 {key} 需要非空文本")
    return value.strip()


def indexed(data):
    items = data.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("至少需要一个必验项")
    result = {}
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("必验项必须为对象")
        key = required(item, "id")
        if key in result:
            raise ValueError(f"重复的验收项：{key}")
        result[key] = item
    return result


def plan(data):
    if data.get("delivery") not in ("review", "merge"):
        raise ValueError("交付目标须为 review 或 merge")
    if data["delivery"] == "merge":
        required(data, "base")
    items = indexed(data)
    for item in items.values():
        required(item, "input")
        required(item, "expected")
    return items


def candidate(data):
    required(data, "self_test")
    required(data, "evidence")
    return required(data, "version")


def evaluate(spec, dev, qa):
    expected = plan(spec)
    version = candidate(dev)
    if required(qa, "version") != version:
        raise ValueError("复验版本与开发交付的候选版本不一致")
    results = indexed(qa)
    if results.keys() != expected.keys():
        raise ValueError(
            "复验项与开工前的清单不一致；缺少："
            + "、".join(expected.keys() - results.keys())
            + "；多出："
            + "、".join(results.keys() - expected.keys())
        )
    rows = []

    def append(label, result, definition=None):
        if not isinstance(result, dict) or result.get("status") not in (
            "通过",
            "失败",
            "未测",
        ):
            raise ValueError(f"{label} 需要通过、失败或未测结论")
        actual = required(result, "actual")
        evidence = (
            required(result, "evidence")
            if result["status"] != "未测"
            else result.get("evidence", "")
        )
        if not isinstance(evidence, str):
            raise ValueError(f"{label} 的证据位置须为文本")
        rows.append(
            {
                **(definition or {}),
                "id": label,
                "status": result["status"],
                "actual": actual,
                "evidence": evidence,
            }
        )

    for key, item in expected.items():
        append(key, results[key], item)
    append("代码评审", qa.get("review"))
    if spec["delivery"] == "merge":
        merge = qa.get("merge")
        if not isinstance(merge, dict):
            raise ValueError("合并前须填写 CI、产品签收及合并授权")
        for key, label in (
            ("ci", "必要 CI"),
            ("approval", "产品签收"),
            ("authorization", "合并授权"),
        ):
            append(label, merge.get(key))
        if merge["ci"]["status"] == "通过" and merge["ci"].get("version") != version:
            raise ValueError("必要 CI 的通过记录不属于当前候选版本")
    failed = [row["id"] for row in rows if row["status"] == "失败"]
    missing = [row["id"] for row in rows if row["status"] == "未测"]
    # 签收与授权未获批须等待决定人，不能由开发者返修改写。
    technical_failure = any(
        row["status"] == "失败" for row in rows[: len(expected) + 1]
    )
    if spec["delivery"] == "merge" and qa["merge"]["ci"]["status"] == "失败":
        technical_failure = True
    action = (
        "rework"
        if technical_failure
        else "needs_input"
        if failed or missing
        else "done"
    )
    return {
        "passed": action == "done",
        "action": action,
        "version": version,
        "delivery": spec["delivery"],
        "rows": rows,
        "reason": "、".join(failed + missing),
    }


def display(steps):
    artifacts = {
        s["position"]: s["output_artifact"] for s in steps
        if "bosun-acceptance" in (s["output_artifact"] or "")
    }
    view = {"rows": [], "version": "", "message": "等待需求登记必验项"}
    if 1 not in artifacts:
        return view
    try:
        spec = record(artifacts[1])
        view["rows"] = [
            {
                "id": key,
                "input": item["input"],
                "expected": item["expected"],
                "status": "未测",
                "actual": "",
                "evidence": "",
            }
            for key, item in plan(spec).items()
        ]
        view["message"] = "等待开发交付候选版本"
        if 2 in artifacts:
            dev = record(artifacts[2])
            view["version"] = candidate(dev)
            view["message"] = "等待独立复验"
            if 3 in artifacts:
                verdict = evaluate(spec, dev, record(artifacts[3]))
                view["rows"] = verdict["rows"]
                view["message"] = (
                    "验收通过"
                    if verdict["passed"]
                    else "尚未通过：" + verdict["reason"]
                )
    except ValueError as exc:
        view["message"] = str(exc)
    return view


def read_pull_request(project_path, number):
    """从本项目关联的 GitHub 仓库读取回执，不执行合并。"""
    if type(number) is not int or number < 1:
        raise ValueError("merge.pr 需要本仓库的 PR 编号")
    fields = "number,url,headRefOid,baseRefName,state,isDraft,reviewDecision,mergeStateStatus,mergeCommit"
    try:
        proc = subprocess.run(
            ["gh", "pr", "view", str(number), "--json", fields],
            cwd=project_path,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("无法读取 GitHub PR，请检查项目目录、gh 及网络后重试") from exc
    if proc.returncode:
        raise ValueError("读取 GitHub PR 失败，请核对仓库、PR 编号和 gh 登录状态")
    return json.loads(proc.stdout)
