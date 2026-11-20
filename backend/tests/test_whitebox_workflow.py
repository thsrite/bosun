"""白盒工作流策略引擎、Diff 反作弊扫描、图谱投影、签收与证据在线预览测试。"""
from __future__ import annotations

import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from unittest.mock import patch

ROOT = str(Path(__file__).resolve().parents[2])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fastapi.testclient import TestClient


class WhiteboxWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo_dir = Path(self.tmp.name) / "repo"
        self.repo_dir.mkdir(parents=True, exist_ok=True)

        from backend.app import config, db, main
        self._old_data_dir = config.DATA_DIR
        config.DATA_DIR = Path(self.tmp.name) / "data"
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._old_conn = db._conn
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        db._conn = self.conn
        db.init_db()

        self.db = db
        self.client = TestClient(main.app)
        self.project_id = db.execute(
            "INSERT INTO project(name,path,created_at) VALUES(?,?,?)",
            ("demo-repo", str(self.repo_dir), time.time()),
        )

    def tearDown(self):
        from backend.app import config
        config.DATA_DIR = self._old_data_dir
        self.conn.close()
        self.db._conn = self._old_conn
        self.tmp.cleanup()

    def _init_git_repo(self) -> str:
        subprocess.run(["git", "init", "-q"], cwd=self.repo_dir, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.repo_dir, check=True)
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=self.repo_dir, check=True)
        (self.repo_dir / "app.py").write_text("def ok():\n    return 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.repo_dir, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=self.repo_dir, check=True)
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.repo_dir, text=True).strip()

    def test_policy_crud_and_l3_merge_evaluation(self):
        from backend.app import workflow_policy

        res = self.client.get(f"/api/projects/{self.project_id}/workflow-policy")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["trust_level"], 1)
        self.assertEqual(body["level_code"], "L1")

        upd = self.client.put(
            f"/api/projects/{self.project_id}/workflow-policy",
            json={
                "trust_level": 3,
                "diff_integrity_gate": True,
                "require_product_signoff": True,
                "auto_merge_globs": ["docs/**", "frontend/src/*.css"],
            },
        )
        self.assertEqual(upd.status_code, 200)
        policy = upd.json()
        self.assertEqual(policy["trust_level"], 3)
        self.assertEqual(policy["auto_merge_globs"], ["docs/**", "frontend/src/*.css"])

        in_scope = workflow_policy.evaluate_merge_policy(policy, ["docs/guide.md"])
        self.assertTrue(in_scope["auto_merge"])
        out_scope = workflow_policy.evaluate_merge_policy(policy, ["backend/auth.py"])
        self.assertFalse(out_scope["auto_merge"])
        self.assertIn("backend/auth.py", out_scope["reason"])

    def test_scan_diff_integrity_catches_scope_and_anti_cheat(self):
        from backend.app import workflow_policy

        baseline = self._init_git_repo()
        (self.repo_dir / "app.py").write_text(
            "import pytest\n@pytest.mark.skip(reason='later')\ndef ok():\n    try:\n        return 1\n    except Exception: pass\n",
            encoding="utf-8",
        )
        (self.repo_dir / "secret.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.repo_dir, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "bad"], cwd=self.repo_dir, check=True)
        candidate = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.repo_dir, text=True).strip()

        report = workflow_policy.scan_diff_integrity(
            str(self.repo_dir), baseline, candidate, allowed_write_globs=["app.py"]
        )
        self.assertTrue(report["checked"])
        self.assertFalse(report["passed"])
        self.assertEqual(report["out_of_scope"], ["secret.py"])
        kinds = {f["kind"] for f in report["findings"]}
        self.assertIn("test_skip", kinds)
        self.assertIn("empty_except", kinds)

    def test_trace_graph_signoff_and_receipt_files(self):
        from backend.app import orchestrations

        tpl = orchestrations.create_template(
            "三角色白盒试行",
            [
                {"name": "需求 AI", "engine": "claude", "role_prompt": "整理需求"},
                {"name": "开发 AI", "engine": "codex", "role_prompt": "实现自测"},
                {"name": "质量 AI", "engine": "claude", "role_prompt": "独立验收"},
            ],
        )
        with patch("backend.app.orchestrations.scheduler.tick"):
            run = orchestrations.create_run(
                tpl["id"], self.project_id, "导出筛选结果", "导出任务", 5, False, start=True
            )

        # 验证编排自动创建唯一主任务，且所有角色步骤均作为其 Bosun 子任务挂在下面
        run_row = self.db.query_one("SELECT root_task_id, status FROM orchestration_run WHERE id=?", (run["id"],))
        self.assertIsNotNone(run_row["root_task_id"])
        root_task = self.db.query_one("SELECT * FROM task WHERE id=?", (run_row["root_task_id"],))
        self.assertIsNone(root_task["parent_task_id"])
        self.assertEqual(root_task["orchestration_run_id"], run["id"])
        self.assertEqual(root_task["status"], "running")

        step_tasks = self.db.query(
            "SELECT t.* FROM task t JOIN orchestration_step_run s ON s.task_id=t.id WHERE s.run_id=?",
            (run["id"],),
        )
        self.assertGreaterEqual(len(step_tasks), 1)
        for st in step_tasks:
            self.assertEqual(st["parent_task_id"], root_task["id"])
            self.assertEqual(st["orchestration_run_id"], run["id"])

        # 验证 /api/tasks/{id}/family 返回主任务、子任务与 orchestration_run_id
        fam = self.client.get(f"/api/tasks/{root_task['id']}/family")
        self.assertEqual(fam.status_code, 200)
        fam_body = fam.json()
        self.assertEqual(fam_body["parent"]["id"], root_task["id"])
        self.assertEqual(fam_body["orchestration_run_id"], run["id"])
        self.assertGreaterEqual(len(fam_body["subtasks"]), 1)

        # 模拟一条返工消息，验证图谱生成返工边
        self.db.execute(
            "INSERT INTO orchestration_message(run_id,from_position,to_position,kind,body,created_at) VALUES(?,?,?,?,?,?)",
            (run["id"], 3, 2, "rework", "缺少第三笔订单", time.time()),
        )

        # 模拟一条带 zip 证据包的远程回执，测试在线文件列表与安全预览
        step_row = self.db.query_one("SELECT id FROM orchestration_step_run WHERE run_id=? AND position=1", (run["id"],))
        receipt_id = self.db.execute(
            "INSERT INTO delivery_check(step_id,attempt,version,items,host,command,state,detail) VALUES(?,?,?,?,?,?,?,?)",
            (step_row["id"], 1, "a" * 40, json.dumps(["A1"]), "remote-1", "pytest", "passed", "ok"),
        )
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("A1.actual.png", b"\x89PNG\r\n\x1a\nfake")
            zf.writestr("A1.json", json.dumps({"observed": 3}))
        self.db.execute(
            "INSERT INTO delivery_report(receipt,outcomes,archive) VALUES(?,?,?)",
            (receipt_id, json.dumps({"A1": ["passed", "ok"]}), buf.getvalue()),
        )

        # 退回签收不填理由应返回 400
        bad = self.client.post(f"/api/orchestration-runs/{run['id']}/signoff", json={"decision": "rejected_impl"})
        self.assertEqual(bad.status_code, 400)

        # 合法退回签收
        rej = self.client.post(
            f"/api/orchestration-runs/{run['id']}/signoff",
            json={"decision": "rejected_impl", "signer": "张三", "comment": "表头缺含税字样"},
        )
        self.assertEqual(rej.status_code, 200)

        # 读取 trace，检查 graph 结构、tickets 进度与 artifacts 索引
        trace_res = self.client.get(f"/api/orchestration-runs/{run['id']}/trace")
        self.assertEqual(trace_res.status_code, 200)
        trace = trace_res.json()
        graph = trace["graph"]
        node_ids = {n["id"] for n in graph["nodes"]}
        self.assertTrue({"sys:intake", "step:1", "step:2", "step:3", "human:signoff", "sys:merge"}.issubset(node_ids))
        edge_ids = {e["id"] for e in graph["edges"]}
        self.assertIn("e:rework:3->2", edge_ids)
        self.assertIn("e:signoff:rejected_impl", edge_ids)
        self.assertIn("tickets", graph)
        self.assertEqual(graph["tickets"]["total"], 4)
        self.assertIn("artifacts", graph)

        # 验证超长阶段产物走「共享黑板落盘 + 微型信封交棒」
        long_artifact = "# 需求单\n" + ("详细背景说明 " * 200)
        step1 = self.db.query_one("SELECT * FROM orchestration_step_run WHERE run_id=? AND position=1", (run["id"],))
        handoff = orchestrations._handoff_body(step1, json.loads(run["definition_snapshot"]), long_artifact)
        self.assertIn("微型信封", handoff)
        self.assertIn("step-1-r1.md", handoff)
        self.assertLess(len(handoff), 500)

        # 证据文件列表与读取
        files_res = self.client.get(f"/api/orchestration-runs/{run['id']}/receipts/{receipt_id}/files")
        self.assertEqual(files_res.status_code, 200)
        names = [f["name"] for f in files_res.json()["files"]]
        self.assertEqual(names, ["A1.actual.png", "A1.json"])

        png_res = self.client.get(f"/api/orchestration-runs/{run['id']}/receipts/{receipt_id}/file/A1.actual.png")
        self.assertEqual(png_res.status_code, 200)
        self.assertTrue(png_res.content.startswith(b"\x89PNG"))

        # 目录穿越应被拦截
        traversal = self.client.get(f"/api/orchestration-runs/{run['id']}/receipts/{receipt_id}/file/../secret")
        self.assertIn(traversal.status_code, (400, 404))


if __name__ == "__main__":
    unittest.main()
