"""逐项校对 JUnit 用例与任务单，生成可留存的回执。"""
import hashlib
import io
import json
from pathlib import Path
import shlex
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile

from . import specialist


def evidence(archive, key, item):
    if item["area"] == "ui":
        reference = archive.read(key + ".reference.png")
        actual = archive.read(key + ".actual.png")
        if not all(p.startswith(b"\x89PNG\r\n\x1a\n") and len(p) >= 33 for p in (reference, actual)):
            raise ValueError("缺少可识别的参照图或实现图")
        if hashlib.sha256(reference).hexdigest() != item["reference_sha256"]:
            raise ValueError("参照图与开工前认可的版本不一致")
        comparison = json.loads(archive.read(key + ".visual.json"))
        if "error" in comparison:
            raise ValueError("无法比较像素：" + comparison["error"])
        if comparison.get("different_size"):
            return "failed", f"参照与实现的尺寸不同：{comparison['sizes']}"
        ratio = comparison["changed"] / comparison["pixels"]
        limit = item.get("max_diff_ratio", 0)
        return ("passed" if ratio <= limit else "failed",
                f"不同像素 {comparison['changed']}/{comparison['pixels']}，允许比例 {limit:.3%}")
    if item["area"] in ("db", "api", "integration", "perf"):
        data = json.loads(archive.read(key + ".json"))
        if not isinstance(data, dict):
            raise ValueError("缺少专项实际结果")
        if item["area"] == "perf":
            budget = item["budget"]
            if (not all(specialist.number(data.get(k)) for k in ("baseline", "candidate"))
                    or data.get("load") != item["workload"] or any(data.get(k) != budget[k] for k in ("metric", "unit"))):
                raise ValueError("性能报告的负载、基线、候选值或指标单位与预算不符")
            message = f"{budget['metric']}：基线 {data['baseline']}，候选 {data['candidate']}，上限 {budget['max']} {budget['unit']}"
            return ("failed" if data["candidate"] > budget["max"] else "passed", message)
        if "observed" not in data:
            raise ValueError("专项报告缺少 observed 实际读回")
        field = {"db": "engine", "api": "request", "integration": "record"}[item["area"]]
        if not isinstance(data.get(field), str) or not data[field].strip():
            raise ValueError(f"专项报告缺少 {field}")
        if item["area"] == "api" and (type(data.get("status")) is not int or not 100 <= data["status"] <= 599):
            raise ValueError("缺少实际 HTTP 状态码")
    return "passed", "用例及所需证据齐全"


def evaluate(payload, items, position):
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        if sum(f.file_size for f in archive.infolist()) > 33 * 1024 * 1024:
            raise ValueError("证据包过大")
        execution = json.loads(archive.read("execution.json"))
        outcomes = {}
        try:
            cases = list(ET.fromstring(archive.read("report.xml")).iter("testcase"))
        except (KeyError, ET.ParseError) as exc:
            return {k: ("error", "缺少本轮有效的 JUnit 报告：" + str(exc)) for k in items}
        matched = set()
        for key, item in items.items():
            name = item.get("qa_case", item["case"]) if position == 3 else item["case"]
            found = [c for c in cases if name in (c.get("name"), f"{c.get('classname','')}::{c.get('name')}")]
            if len(found) != 1 or id(found[0]) in matched:
                outcomes[key] = ("error", f"报告中无法唯一对应独立用例 {name}")
                continue
            case = found[0]
            matched.add(id(case))
            if case.find("failure") is not None or case.find("error") is not None:
                failure = next(n for n in case if n.tag in ("failure", "error"))
                outcomes[key] = ("failed", f"{name} 失败：" + (failure.get("message") or failure.text or "")[:900])
            elif case.find("skipped") is not None or case.get("status") in ("disabled", "notrun"):
                outcomes[key] = ("error", f"{name} 已跳过")
            else:
                try:
                    state, detail = evidence(archive, key, item)
                    outcomes[key] = (state, f"{name} · {detail}")
                except (KeyError, ValueError, UnicodeError) as exc:
                    outcomes[key] = ("error", f"{name} 缺少可核对证据：{exc}")
        if execution["code"] and not any(state == "failed" for state, _ in outcomes.values()):
            outcomes = {k: ("error", f"测试命令退出码 {execution['code']}；{v[1]}") for k, v in outcomes.items()}
        return outcomes


def collect(host, directory, version, items, position, command, collector_python):
    script = Path(__file__).resolve().parents[1] / "collect_delivery_report.py"
    check_runner = "yes" if host.rsplit("@", 1)[-1] == "10.0.0.50" else "no"
    areas = {key: item["area"] for key, item in items.items()}
    args = [collector_python, "-", directory, version, check_runner, json.dumps(areas), *command]
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        proc = subprocess.run(["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                               "--", host, shlex.join(args)], input=script.read_bytes(), stdout=output, stderr=errors)
        if proc.returncode or output.tell() > 33 * 1024 * 1024:
            errors.seek(max(0, errors.tell() - 1024))
            raise ValueError("远程证据收集未完成：" + errors.read().decode(errors="replace"))
        output.seek(0)
        payload = output.read()
    return evaluate(payload, items, position), payload
