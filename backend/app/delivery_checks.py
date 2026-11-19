"""将远程测试回执绑定到角色、本轮尝试、候选提交和验收项。"""
import json

from . import db


def recent(step, version):
    rows = db.query(
        "SELECT * FROM delivery_check WHERE step_id=? AND attempt=? AND version=? ORDER BY id DESC",
        (step["id"], step["attempt"], version),
    )
    by_item = {}
    for row in rows:
        for item in json.loads(row["items"]):
            by_item.setdefault(item, row)
    return by_item


def results(step, spec, version):
    receipts = recent(step, version)
    rows = []
    for item in spec["items"]:
        key = item["id"].strip()
        receipt = receipts.get(key)
        state = receipt["state"] if receipt else "missing"
        label = "开发自测" if step["position"] == 2 else "独立复测"
        rows.append({
            "id": f"{label} · {key}",
            "status": {"passed": "通过", "failed": "失败"}.get(state, "未测"),
            "actual": (receipt["detail"] or "执行中") if receipt else "尚无本轮候选提交的执行记录",
            "evidence": (f"回执 #{receipt['id']} · {receipt['host']} · {receipt['command']} · {version}"
                         if receipt else ""),
        })
    return rows
