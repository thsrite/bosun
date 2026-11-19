"""通过 SSH 标准输入运行。仅从本轮新建目录收集指定用例的脱敏证据。"""
import json
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile


def compare_images(reference, actual):
    from PIL import Image, ImageChops
    with Image.open(reference, formats=["PNG"]) as before, Image.open(actual, formats=["PNG"]) as after:
        if before.size != after.size:
            return {"different_size": True, "sizes": [before.size, after.size]}, None
        difference = ImageChops.difference(before.convert("RGBA"), after.convert("RGBA"))
        channels = difference.split()
        mask = channels[0]
        for channel in channels[1:]:
            mask = ImageChops.lighter(mask, channel)
        count = before.width * before.height
        changed = count - mask.histogram()[0]
        difference_png = None
        if changed:
            buffer = io.BytesIO()
            mask.point(lambda value: 255 if value else 0).save(buffer, format="PNG")
            difference_png = buffer.getvalue()
        return {"pixels": count, "changed": changed}, difference_png


def collect(directory, version, busy_check, item_ids, command):
    os.chdir(directory)
    if busy_check and subprocess.run(["pgrep", "-f", "[R]unner.Worker"], stdout=subprocess.DEVNULL).returncode == 0:
        raise ValueError("测试机正在执行构建任务")

    def verify():
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        if head != version or subprocess.run(["git", "diff", "--quiet", "HEAD", "--"]).returncode:
            raise ValueError("测试副本与候选提交不一致")

    verify()
    if subprocess.check_output(["git", "ls-files", "--others", "--exclude-standard", "--", ":/"]):
        raise ValueError("请准备干净的远程测试副本")
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryFile() as log:
        root = Path(tmp)
        report = str(root / "report.xml")
        env = dict(os.environ, BOSUN_EVIDENCE_DIR=tmp, BOSUN_REPORT=report, PLAYWRIGHT_JUNIT_OUTPUT_FILE=report)
        args = [a.replace("{report}", report).replace("{evidence}", tmp) for a in command]
        code = subprocess.run(args, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log).returncode
        verify()
        log.seek(max(log.tell() - 8192, 0))
        filenames = ["report.xml"] + [f"{key}.{ext}" for key in item_ids
                                      for ext in ("reference.png", "actual.png", "json")]
        with zipfile.ZipFile(sys.stdout.buffer, "w") as archive:
            archive.writestr("execution.json", json.dumps({
                "version": version, "command": command, "checks": item_ids,
                "code": code, "log": log.read().decode(errors="replace"),
            }, ensure_ascii=False))
            size = 0
            for name in filenames:
                p = root / name
                if p.is_symlink() or not p.is_file():
                    continue
                size += p.stat().st_size
                if size > 32 * 1024 * 1024:
                    raise ValueError("单次证据超过 32 MiB，请按验收项拆分或裁剪截图")
                archive.write(p, name)
            for key, area in item_ids.items():
                if area != "ui":
                    continue
                before, after = (root / f"{key}.{suffix}.png" for suffix in ("reference", "actual"))
                if all(p.is_file() and not p.is_symlink() for p in (before, after)):
                    try:
                        comparison, difference_png = compare_images(before, after)
                    except (ImportError, OSError, ValueError) as exc:
                        comparison, difference_png = {"error": str(exc)}, None
                    if difference_png:
                        size += len(difference_png)
                        if size > 32 * 1024 * 1024:
                            raise ValueError("差异图超出单次证据大小，请裁剪至验收区域")
                        archive.writestr(key + ".diff.png", difference_png)
                    archive.writestr(key + ".visual.json", json.dumps(comparison))


if __name__ == "__main__":
    collect(sys.argv[1], sys.argv[2], sys.argv[3] == "yes", json.loads(sys.argv[4]), sys.argv[5:])
