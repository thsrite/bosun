"""GET /api/tasks/{id}/file：终端里双击文件后的取文件接口。"""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.routers import tasks as tasks_router


class TaskFileRouteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        (self.root / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        (self.root / "notes.md").write_text("# 标题\n正文\n", encoding="utf-8")
        (self.root / "page.html").write_text("<script>alert(1)</script>", encoding="utf-8")
        (self.root / "bundle.zip").write_bytes(b"PK\x03\x04")

        app = FastAPI()
        app.include_router(tasks_router.router)
        self.client = TestClient(app)
        patcher = patch.object(
            tasks_router.db, "query_one", return_value={"root": str(self.root)}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def get(self, path: str, **params):
        return self.client.get(f"/api/tasks/1/file", params={"path": path, **params})

    def test_image_is_served_with_its_own_type_and_kind(self):
        res = self.get("shot.png")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers["X-Preview-Kind"], "image")
        self.assertEqual(res.headers["content-type"], "image/png")
        self.assertEqual(res.content, b"\x89PNG\r\n\x1a\n")

    def test_text_is_served_as_utf8_plain_text(self):
        res = self.get("notes.md")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers["X-Preview-Kind"], "text")
        self.assertEqual(res.headers["content-type"], "text/plain; charset=utf-8")
        self.assertIn("标题", res.text)

    def test_html_is_never_served_as_html(self):
        # 否则任务目录里 agent 刚写的 html 会在应用同源下执行
        res = self.get("page.html")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers["content-type"], "text/plain; charset=utf-8")
        self.assertEqual(res.headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("sandbox", res.headers["Content-Security-Policy"])

    def test_binary_is_forced_to_download(self):
        res = self.get("bundle.zip")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers["X-Preview-Kind"], "binary")
        self.assertIn("attachment", res.headers["content-disposition"])

    def test_download_flag_forces_attachment_for_previewable_types(self):
        res = self.get("shot.png", download="true")
        self.assertIn("attachment", res.headers["content-disposition"])

    def test_previewable_types_are_inline_by_default(self):
        self.assertNotIn("content-disposition", self.get("shot.png").headers)

    def test_path_outside_workdir_is_refused(self):
        res = self.get("/etc/passwd")
        self.assertEqual(res.status_code, 403)

    def test_dot_dot_escape_is_refused(self):
        res = self.get("../../etc/passwd")
        self.assertEqual(res.status_code, 403)

    def test_missing_file_is_404(self):
        self.assertEqual(self.get("nope.txt").status_code, 404)

    def test_unknown_task_is_404(self):
        with patch.object(tasks_router.db, "query_one", return_value=None):
            self.assertEqual(self.get("notes.md").status_code, 404)


if __name__ == "__main__":
    unittest.main()
