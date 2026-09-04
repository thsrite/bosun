import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from backend.app.task_files import (
    MAX_PREVIEW_BYTES,
    TaskFileError,
    preview_kind,
    resolve_task_file,
    response_media_type,
)


class ResolveTaskFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        (self.root / "sub").mkdir()
        (self.root / "sub" / "shot.png").write_bytes(b"\x89PNG\r\n")
        (self.root / "notes.md").write_text("hi")

    def test_relative_path_resolves_under_root(self):
        self.assertEqual(resolve_task_file(self.root, "sub/shot.png"), self.root / "sub" / "shot.png")

    def test_dot_slash_prefix_is_accepted(self):
        self.assertEqual(resolve_task_file(self.root, "./notes.md"), self.root / "notes.md")

    def test_absolute_path_inside_root_is_accepted(self):
        target = str(self.root / "notes.md")
        self.assertEqual(resolve_task_file(self.root, target), self.root / "notes.md")

    def test_absolute_path_outside_root_is_refused(self):
        with self.assertRaises(TaskFileError) as ctx:
            resolve_task_file(self.root, "/etc/passwd")
        self.assertEqual(ctx.exception.status, 403)

    def test_dot_dot_escape_is_refused(self):
        with self.assertRaises(TaskFileError) as ctx:
            resolve_task_file(self.root, "../../etc/passwd")
        self.assertEqual(ctx.exception.status, 403)

    def test_symlink_pointing_outside_root_is_refused(self):
        outside = Path(self.tmp.name).resolve().parent / "bosun-test-outside.txt"
        outside.write_text("secret")
        self.addCleanup(outside.unlink)
        link = self.root / "escape.txt"
        link.symlink_to(outside)
        with self.assertRaises(TaskFileError) as ctx:
            resolve_task_file(self.root, "escape.txt")
        self.assertEqual(ctx.exception.status, 403)

    def test_missing_file_is_404(self):
        with self.assertRaises(TaskFileError) as ctx:
            resolve_task_file(self.root, "nope.txt")
        self.assertEqual(ctx.exception.status, 404)

    def test_directory_is_not_a_file(self):
        with self.assertRaises(TaskFileError) as ctx:
            resolve_task_file(self.root, "sub")
        self.assertEqual(ctx.exception.status, 404)

    def test_empty_path_is_rejected(self):
        with self.assertRaises(TaskFileError) as ctx:
            resolve_task_file(self.root, "   ")
        self.assertEqual(ctx.exception.status, 400)

    def test_oversized_file_is_refused(self):
        big = self.root / "big.bin"
        big.write_bytes(b"0" * (MAX_PREVIEW_BYTES + 1))
        with self.assertRaises(TaskFileError) as ctx:
            resolve_task_file(self.root, "big.bin")
        self.assertEqual(ctx.exception.status, 413)

    def test_root_itself_is_refused_as_a_file(self):
        with self.assertRaises(TaskFileError) as ctx:
            resolve_task_file(self.root, ".")
        self.assertEqual(ctx.exception.status, 404)

    def test_sibling_directory_with_shared_prefix_is_refused(self):
        # /work 与 /work-secrets 前缀相同但不是子目录，纯字符串前缀判断会漏
        sibling = self.root.parent / (self.root.name + "-secrets")
        sibling.mkdir()
        leak = sibling / "key.txt"
        leak.write_text("secret")
        self.addCleanup(lambda: (leak.unlink(), sibling.rmdir()))
        with self.assertRaises(TaskFileError) as ctx:
            resolve_task_file(self.root, str(leak))
        self.assertEqual(ctx.exception.status, 403)


class PreviewKindTest(unittest.TestCase):
    def test_images(self):
        for name in ("a.png", "a.JPG", "a.jpeg", "a.gif", "a.webp", "a.svg", "a.avif"):
            self.assertEqual(preview_kind(Path(name)), "image", name)

    def test_pdf(self):
        self.assertEqual(preview_kind(Path("report.pdf")), "pdf")

    def test_text_like(self):
        for name in ("a.md", "a.json", "a.log", "a.py", "a.tsx", "a.yaml", "a.csv", "a.txt"):
            self.assertEqual(preview_kind(Path(name)), "text", name)

    def test_extensionless_known_names_are_text(self):
        for name in ("Dockerfile", "Makefile", ".gitignore", "LICENSE"):
            self.assertEqual(preview_kind(Path(name)), "text", name)

    def test_unknown_falls_back_to_binary(self):
        for name in ("a.zip", "a.bin", "a.mysteryext"):
            self.assertEqual(preview_kind(Path(name)), "binary", name)

    def test_text_like_files_are_served_as_plain_text(self):
        # .html/.svg 若按自身 MIME 内联返回，等于在应用同源下执行任意脚本
        self.assertEqual(response_media_type(Path("page.html"), "text"), "text/plain; charset=utf-8")

    def test_images_keep_their_own_media_type(self):
        self.assertEqual(response_media_type(Path("a.png"), "image"), "image/png")

    def test_pdf_keeps_its_media_type(self):
        self.assertEqual(response_media_type(Path("a.pdf"), "pdf"), "application/pdf")

    def test_binary_is_octet_stream(self):
        self.assertEqual(response_media_type(Path("a.zip"), "binary"), "application/octet-stream")


if __name__ == "__main__":
    unittest.main()
