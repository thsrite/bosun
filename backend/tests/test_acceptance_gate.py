import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app import db, orchestrations, scheduler
from app.routers import tasks
from app.routers.orchestrations import AcceptanceSetting, configure_acceptance
from fastapi import HTTPException
import run_delivery_check


def artifact(data):
    return (
        "交接记录\n\x60\x60\x60bosun-acceptance\n"
        + json.dumps(data, ensure_ascii=False)
        + "\n\x60\x60\x60"
    )


class AcceptanceGateTest(unittest.TestCase):
    def setUp(self):
        old = db._conn
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        db._conn = self.conn
        self.addCleanup(setattr, db, "_conn", old)
        self.addCleanup(self.conn.close)
        db.init_db()
        project = db.execute(
            "INSERT INTO project(name,path,created_at) VALUES(?,?,?)",
            ("试点", "/tmp/demo", time.time()),
        )
        steps = [
            dict(name=name, engine="codex", role_prompt=name)
            for name in ("需求", "开发", "质量")
        ]
        self.template = orchestrations.create_template("交付", steps)["id"]
        self.project = project
        for name in (
            "start_orchestration_role",
            "finish_orchestration_step",
            "finish_subtask",
            "role_online",
            "deliver_message",
        ):
            p = patch.object(scheduler, name, return_value=True)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(tasks, "_report_authorized", return_value=True)
        p.start()
        self.addCleanup(p.stop)
        self.spec = {
            "delivery": "review",
            "items": [
                {"id": x, "input": "合成样本", "expected": "预期" + x}
                for x in ("A", "B")
            ],
        }
        self.dev = {
            "version": "head1",
            "self_test": "执行两例",
            "evidence": "自测记录#1",
        }
        self.ok = {"status": "通过", "actual": "实际读回", "evidence": "记录#2"}
        self.qa = {
            "version": "head1",
            "items": [dict(self.ok, id=x) for x in ("A", "B")],
            "review": self.ok.copy(),
        }

    def start(self, enabled=True):
        configure_acceptance(self.template, AcceptanceSetting(enabled=enabled))
        run = orchestrations.create_run(
            self.template, self.project, "试点", None, 5, False, True, True
        )
        self.run_id = run["id"]

    def report(self, data, result="done"):
        run = orchestrations.get_run(self.run_id)
        step = run["steps"][run["current_position"] - 1]
        return tasks.report_task(
            step["task_id"], tasks.ReportBody(result=result, artifact=artifact(data))
        )

    def ready(self):
        self.start()
        self.report(self.spec)
        self.report(self.dev)

    def test_missing_plan_and_duplicate_ids_cannot_advance(self):
        self.start()
        for spec in ({}, dict(self.spec, items=self.spec["items"] * 2)):
            with self.assertRaises(HTTPException) as err:
                self.report(spec)
            self.assertEqual(err.exception.status_code, 409)
        self.assertEqual(orchestrations.get_run(self.run_id)["current_position"], 1)

    def test_missing_item_old_version_and_empty_evidence_block_before_task_marked_done(
        self,
    ):
        self.ready()
        for qa in (
            dict(self.qa, items=self.qa["items"][:1]),
            dict(self.qa, version="head0"),
            dict(self.qa, review=dict(self.ok, evidence=" ")),
        ):
            with self.assertRaises(HTTPException):
                self.report(qa)
        run = orchestrations.get_run(self.run_id)
        self.assertEqual(run["current_position"], 3)
        self.assertIsNone(
            db.query_one(
                "SELECT report_result FROM task WHERE id=?",
                (run["steps"][2]["task_id"],),
            )[0]
        )
        self.report(self.qa)
        self.assertEqual(
            orchestrations.get_run(self.run_id)["acceptance"]["message"], "验收通过"
        )

    def test_review_failure_returns_to_dev_and_archives_old_evidence(self):
        self.ready()
        self.qa["review"]["status"] = "失败"
        response = self.report(self.qa)
        self.assertEqual(response["report_result"], "rework")
        run = orchestrations.get_run(self.run_id)
        self.assertEqual(run["current_position"], 2)
        self.assertIn("失败", run["acceptance"]["previous_review"])
        self.assertIsNone(run["steps"][2]["output_artifact"])
        self.report(dict(self.dev, version="head2"))
        with self.assertRaises(HTTPException):
            self.report(self.qa)
        self.qa.update(version="head2", review=self.ok.copy())
        self.report(self.qa)
        self.assertEqual(orchestrations.get_run(self.run_id)["current_position"], 4)

    def test_untested_waits_and_preserves_actual_reason(self):
        self.ready()
        self.qa["items"][0].update(status="未测", actual="远程测试库离线", evidence="")
        self.assertEqual(self.report(self.qa)["report_result"], "needs_input")
        run = orchestrations.get_run(self.run_id)
        self.assertEqual(run["status"], "waiting_input")
        self.assertEqual(run["acceptance"]["rows"][0]["actual"], "远程测试库离线")
        self.qa["items"][0].update(self.ok)
        self.report(self.qa)
        self.assertEqual(orchestrations.get_run(self.run_id)["current_position"], 4)

    def test_merge_preflight_and_receipt_bound_to_candidate(self):
        self.spec.update(delivery="merge", base="main")
        self.ready()
        task_id = orchestrations.get_run(self.run_id)["steps"][2]["task_id"]
        self.qa["merge"] = {
            key: self.ok.copy() for key in ("ci", "approval", "authorization")
        }
        self.qa["merge"]["pr"] = 12
        self.qa["merge"]["ci"]["version"] = "old"
        with self.assertRaises(HTTPException):
            self.report(self.qa)
        self.qa["merge"]["ci"]["version"] = "head1"
        self.qa["merge"]["authorization"]["status"] = "未测"
        self.assertEqual(self.report(self.qa)["report_result"], "needs_input")
        self.qa["merge"]["authorization"].update(self.ok)
        orchestrations.save_task_artifact(task_id, artifact(self.qa))
        with self.assertRaises(HTTPException):
            tasks.check_before_merge(task_id, "old", None)
        pr = dict(
            headRefOid="old",
            number=12,
            baseRefName="main",
            state="OPEN",
            isDraft=False,
            reviewDecision="APPROVED",
            mergeStateStatus="CLEAN",
        )
        with patch.object(tasks.acceptance, "read_pull_request", return_value=pr):
            with self.assertRaises(HTTPException):
                tasks.check_before_merge(
                    task_id, "head1", SimpleNamespace(method="GET")
                )
            pr["headRefOid"] = "head1"
            self.assertTrue(
                tasks.check_before_merge(
                    task_id, "head1", SimpleNamespace(method="GET")
                )["passed"]
            )
            with self.assertRaises(HTTPException):
                tasks.check_before_merge(
                    task_id, "head1", SimpleNamespace(method="POST")
                )
            self.qa["merge_receipt"] = {"version": "head1", "evidence": "自报已合并"}
            self.assertEqual(self.report(self.qa)["report_result"], "needs_input")
            pr.update(state="MERGED", mergeCommit={"oid": "merged"})
            tasks.check_before_merge(task_id, "head1", SimpleNamespace(method="POST"))
            self.assertEqual(self.report(self.qa)["report_result"], "done")

    def test_existing_snapshot_stays_ungated(self):
        self.start(False)
        configure_acceptance(self.template, AcceptanceSetting(enabled=True))
        self.report({})
        run = orchestrations.get_run(self.run_id)
        self.assertNotIn("acceptance", run)
        self.assertEqual(run["current_position"], 2)

    def delivery_fixture(self, kind):
        configure_acceptance(self.template, AcceptanceSetting(enabled=True, remote_checks=True))
        self.start()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        repo = Path(folder.name)
        db.execute("UPDATE project SET path=? WHERE id=?", (str(repo), self.project))
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        self.repo = repo
        self.spec.update(kind=kind)
        if kind == "bug":
            with self.assertRaises(HTTPException):
                self.report(self.spec)
            self.spec["reproduction"] = "输入负数时总分被取绝对值；同样本应保留符号"
        self.report(self.spec)

    def commit_sample(self, expression):
        (self.repo / "total.py").write_text("def total(values):\n    return " + expression + "\n")
        subprocess.run(["git", "add", "total.py"], cwd=self.repo, check=True)
        subprocess.run(["git", "-c", "user.email=test@example.invalid", "-c", "user.name=fixture",
                        "commit", "-qm", "更新合计行为"], cwd=self.repo, check=True)
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.repo, text=True).strip()

    def check_sample(self, version, items, assertion, real_host=None):
        run = orchestrations.get_run(self.run_id)
        task_id = run["steps"][run["current_position"] - 1]["task_id"]
        # 默认用相同的 POSIX shell 执行远端载荷。集成环境可用隔离的回环 SSH 服务。
        execute = subprocess.run

        def transport(cmd, **kwargs):
            if cmd[0] == "ssh" and not real_host:
                return execute(["sh", "-c", cmd[-1]], **kwargs)
            return execute(cmd, **kwargs)

        with patch.object(run_delivery_check.subprocess, "run", side_effect=transport):
            return run_delivery_check.run(task_id, real_host or "fixture", str(self.repo), version, items,
                                          ["python3", "-B", "-c", "from total import total; " + assertion])

    def test_feature_requires_execution_for_each_role_and_item(self):
        self.delivery_fixture("feature")
        version = self.commit_sample("sum(values)")
        self.dev["version"] = self.qa["version"] = version
        host = os.getenv("BOSUN_TEST_LOOPBACK_SSH")
        self.assertEqual(self.check_sample(version, ["A"], "assert total([]) == 0", host), 0)
        with self.assertRaises(HTTPException):
            self.report(self.dev)
        self.assertEqual(self.check_sample(version, ["B"], "assert total([2,3]) == 5", host), 0)
        self.report(self.dev)
        self.assertEqual(self.report(self.qa)["report_result"], "needs_input")
        self.assertEqual(self.check_sample(version, ["A", "B"], "assert [total([]),total([6,2])] == [0,8]", host), 0)
        self.report(self.qa)
        self.report({"summary": "已交付"})
        self.assertEqual(orchestrations.get_run(self.run_id)["status"], "done")

    def test_dirty_remote_checkout_cannot_supply_passing_evidence(self):
        self.delivery_fixture("feature")
        version = self.commit_sample("sum(values)")
        (self.repo / "total.py").write_text("def total(values): return 123\n")
        result = self.check_sample(version, ["A"], "assert total([]) == 123", os.getenv("BOSUN_TEST_LOOPBACK_SSH"))
        self.assertEqual(result, 1)
        receipt = db.query_one("SELECT state,detail FROM delivery_check")
        self.assertEqual(receipt["state"], "error")
        self.assertIn("工作树", receipt["detail"])
        with self.assertRaises(HTTPException):
            self.report(dict(self.dev, version=version))

    def test_document_only_handoff_requires_reason_and_preserves_existing_flow(self):
        configure_acceptance(self.template, AcceptanceSetting(enabled=True, remote_checks=True))
        self.start()
        self.spec.update(kind="noncode")
        with self.assertRaises(HTTPException):
            self.report(self.spec)
        self.report(dict(self.spec, reason="仅要求查证已有能力"))
        self.report(self.dev)
        self.report(self.qa)
        self.report({"summary": "已答复"})
        self.assertEqual(orchestrations.get_run(self.run_id)["status"], "done")

    def test_real_failure_reworks_bug_and_requires_fresh_passing_receipts(self):
        self.delivery_fixture("bug")
        first = self.commit_sample("abs(sum(values))")
        host = os.getenv("BOSUN_TEST_LOOPBACK_SSH")
        self.dev["version"] = self.qa["version"] = first
        self.assertEqual(self.check_sample(first, ["A", "B"], "assert [total([]),total([1])] == [0,1]", host), 0)
        self.report(self.dev)
        self.assertEqual(self.check_sample(first, ["B"], "assert total([-2]) == -2", host), 1)
        self.assertEqual(self.report(self.qa)["report_result"], "rework")
        fixed = self.commit_sample("sum(values)")
        self.dev["version"] = self.qa["version"] = fixed
        with self.assertRaises(HTTPException):
            self.report(self.dev)
        for report in (self.dev, self.qa):
            self.assertEqual(self.check_sample(fixed, ["A", "B"], "assert [total([]),total([-2])] == [0,-2]", host), 0)
            self.report(report)
        self.report({"summary": "缺陷已修"})
        run = orchestrations.get_run(self.run_id)
        self.assertEqual((run["status"], run["rework_total"]), ("done", 1))
        self.assertTrue(any("退出码 0" in row["actual"] for row in run["acceptance"]["rows"]))
        self.assertIn("退出码 1", run["acceptance"]["previous_review"])

    def test_specialties_use_actual_reports_scope_images_and_budget(self):
        import hashlib
        import io
        import sys
        import zipfile
        from app.routers.orchestrations import download_report
        from PIL import Image
        from app import specialist
        configure_acceptance(self.template, AcceptanceSetting(enabled=True, remote_checks=True, specialist_checks=True))
        self.start()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.repo = Path(folder.name)
        db.execute("UPDATE project SET path=? WHERE id=?", (folder.name, self.project))
        subprocess.run(["git", "init", "-q", folder.name], check=True)
        self.spec.update(kind="feature", baseline=self.commit_sample("0"))
        image = io.BytesIO()
        Image.new("RGB", (2, 2), "white").save(image, "PNG")
        definitions = [
            dict(id="V", area="ui", reference="认可的测试白色方块", reference_sha256=hashlib.sha256(image.getvalue()).hexdigest()),
            dict(id="A", area="api", caller="测试端"), dict(id="P", area="perf", workload="样本集", budget=dict(metric="p95", unit="ms", max=10)),
            dict(id="C", area="behavior")]
        self.spec["items"] = [dict(d, case="test_" + d["id"], input="样本", expected="符合指定范围") for d in definitions]
        self.report(self.spec)
        (self.repo / "tests_gate.py").write_text("""
import json, os, pytest
from pathlib import Path
from PIL import Image
from total import total
out = Path(os.environ['BOSUN_EVIDENCE_DIR'])
mode = os.getenv('MODE','good')
def test_V():
    for name, color in [('reference', 'white'), ('actual','black' if mode=='bad' else 'white')]:
        Image.new('RGB',(2,2),color).save(out / f'V.{name}.png')
def test_A():
    assert total([2,3]) == 5
    if mode != 'missing':
        (out/'A.json').write_text(json.dumps({'observed':total([2,3]),'request':'样本请求','status':200}))
def test_P():
    (out/'P.json').write_text(json.dumps(dict(baseline=5,candidate=20 if mode=='bad' else 6,metric='p95',unit='ms',load='样本集')))
def test_C():
    if mode=='skip': pytest.skip('条件未满足')
    assert total([]) == 0
""")
        (self.repo / "models.py").write_text("# 此处只变更注释\n")
        subprocess.run(["git", "add", "tests_gate.py", "models.py"], cwd=self.repo, check=True)
        version = self.commit_sample("sum(values)")
        self.qa.update(version=version, items=[dict(self.ok, id=d["id"]) for d in definitions])
        self.dev.update(version=version)
        with self.assertRaisesRegex(ValueError, "models.py"):
            specialist.check_scope(folder.name, self.spec, version, self.dev)
        self.dev["scope_notes"] = {"db": "只改注释"}
        self.qa["scope_notes"] = {"db": "已复核该文件仅有注释"}
        def run_checks(mode, ids):
            run = orchestrations.get_run(self.run_id)
            task = run["steps"][run["current_position"] - 1]["task_id"]
            ssh = os.getenv("BOSUN_TEST_LOOPBACK_SSH")
            execute = subprocess.run
            def transport(cmd, **kwargs):
                if cmd[0] == "ssh" and not ssh:
                    return execute(["sh", "-c", cmd[-1]], **kwargs)
                return execute(cmd, **kwargs)
            cmd = ["env", "PYTHONDONTWRITEBYTECODE=1", f"MODE={mode}", sys.executable, "-m", "pytest",
                   "-q", "-p", "no:cacheprovider", "--junitxml={report}", *["tests_gate.py::test_"+i for i in ids]]
            with patch.object(run_delivery_check.subprocess, "run", side_effect=transport):
                return run_delivery_check.run(task, ssh or "fixture", folder.name, version, ids, cmd, sys.executable)
        ids = ["V","A","P","C"]
        self.assertEqual(run_checks("good", ids), 0)
        self.report(self.dev)
        self.assertEqual(run_checks("bad", ids), 1)
        self.assertEqual(run_checks("good", ["C"]), 0)
        rows = {r["id"]:r for r in orchestrations.get_run(self.run_id)["acceptance"]["rows"]}
        for key in ("V","P"):
            self.assertEqual(rows["独立复测 · "+key]["status"], "失败")
        self.assertEqual(rows["独立复测 · C"]["status"], "通过")
        receipt = rows["独立复测 · V"]["receipt_id"]
        with zipfile.ZipFile(io.BytesIO(download_report(self.run_id, receipt).body)) as z:
            self.assertIn("report.xml", z.namelist())
            self.assertIn("V.diff.png", z.namelist())
            self.assertEqual(json.loads(z.read("V.visual.json"))["changed"],4)
        with self.assertRaises(HTTPException):
            download_report(self.run_id+1,receipt)
        self.assertEqual(self.report(self.qa)["report_result"],"rework")
        self.assertEqual(run_checks("good", ids), 0)
        self.report(self.dev)
        self.assertEqual(run_checks("skip", ["C"]), 1)
        self.assertEqual(run_checks("missing", ["A"]), 1)
        self.assertEqual(self.report(self.qa)["report_result"],"needs_input")
        self.assertEqual(run_checks("good", ids), 0)
        self.report(self.qa)
        self.report({"summary":"完成"})
        self.assertEqual(orchestrations.get_run(self.run_id)["status"],"done")

    def test_structured_evidence_cannot_mix_dimensions_or_skip_database_readback(self):
        import io
        import zipfile
        from app import check_reports, specialist

        def evaluate(items, files):
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, "w") as z:
                z.writestr("execution.json", '{"code":0}')
                z.writestr("report.xml", '<testsuite><testcase name="db"/><testcase name="api"/><testcase name="perf"/></testsuite>')
                for key, values in files.items():
                    z.writestr(key + ".json", json.dumps(values))
            return check_reports.evaluate(stream.getvalue(), items, 2)

        items = {"D": dict(area="db", case="db"), "A": dict(area="api", case="api"),
                 "P": dict(area="perf", case="perf", workload="10000 行", budget=dict(max=1,metric="p95",unit="s"))}
        samples = {"D": {"observed": 0}, "A": {"observed": [], "request": "GET /scores", "status": 200},
                   "P": {"candidate":0.4, "baseline":0.6, "load":"10 行", "metric":"p95","unit":"s"}}
        results = evaluate(items, samples)
        self.assertEqual([v[0] for v in results.values()], ["error","passed","error"])
        samples["D"]["engine"] = "PostgreSQL"
        samples["P"]["load"] = "10000 行"
        self.assertTrue(all(v[0] == "passed" for v in evaluate(items, samples).values()))
        common = dict(input="样本", expected="完成", area="behavior")
        spec = dict(baseline="f"*40, delivery="review",
                    items=[dict(common,id="A",case="click"),dict(common,id="V",case="Page::click")])
        with self.assertRaisesRegex(ValueError,"重复用例"):
            specialist.validate(spec)

    def test_trace_records_rejected_and_rerouted_reports_without_advancing_or_losing_history(self):
        self.ready()
        with self.assertRaises(HTTPException):
            self.report(dict(self.qa, version="different"))
        trace = orchestrations.run_trace(self.run_id)
        denied = trace["decisions"][-1]
        self.assertEqual((denied["requested"], denied["result"], denied["position"]), ("done","rejected",3))
        self.assertIn("版本", denied["reason"])
        self.assertEqual(trace["run"]["current_position"], 3)
        self.qa["review"]["status"] = "失败"
        self.report(self.qa)
        trace = orchestrations.run_trace(self.run_id)
        returned = trace["decisions"][-1]
        self.assertEqual((returned["requested"], returned["result"], returned["attempt"]), ("done","rework",1))
        self.assertIn("评审", returned["basis"])
        self.assertTrue(any(a["position"] == 3 and a["input_artifact"] and a["output_artifact"] for a in trace["archives"]))
        self.assertTrue(any(m["kind"] == "rework" for m in trace["messages"]))
