"""将远程测试回执绑定到角色、本轮尝试、候选提交和验收项。"""
import json

from . import db


def recent(step, version):
    rows = db.query(
        "SELECT c.*, r.outcomes FROM delivery_check c LEFT JOIN delivery_report r ON r.receipt=c.id "
        "WHERE c.step_id=? AND c.attempt=? AND c.version=? ORDER BY c.id DESC",
        (step["id"], step["attempt"], version),
    )
    by_item = {}
    for row in rows:
        for item in json.loads(row["items"]):
            by_item.setdefault(item, row)
    return by_item


def results(step, spec, version, require_report=False):
    receipts = recent(step, version)
    rows = []
    for item in spec["items"]:
        key = item["id"].strip()
        receipt = receipts.get(key)
        state = receipt["state"] if receipt else "missing"
        detail = receipt["detail"] if receipt else "尚无本轮候选提交的执行记录"
        if receipt and receipt["outcomes"]:
            state, detail = json.loads(receipt["outcomes"])[key]
        elif require_report and state == "passed":
            state, detail = "error", "此项只有命令退出码，尚无绑定用例的报告"
        label = "开发自测" if step["position"] == 2 else "独立复测"
        rows.append({
            "id": f"{label} · {key}",
            "status": {"passed": "通过", "failed": "失败"}.get(state, "未测"),
            "actual": detail or "执行中",
            "receipt_id": receipt["id"] if receipt and receipt["outcomes"] else None,
            "evidence": (f"回执 #{receipt['id']} · {receipt['host']} · {receipt['command']} · {version}"
                         if receipt else ""),
        })
    return rows
