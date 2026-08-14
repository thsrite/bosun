# PyInstaller 打包定义：把后端 + 前端构建产物 + 随包 skills 打成免源码运行的 onedir 产物。
# 构建（仓库根执行，需先 npm run build 生成 frontend/dist）：
#   python -m PyInstaller packaging/bosun.spec --noconfirm
# 产物：dist/bosun/bosun（macOS/Linux 同一份 spec）
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

ROOT = Path(SPECPATH).resolve().parent  # noqa: F821  # SPECPATH 由 PyInstaller 注入

dist_dir = ROOT / "frontend" / "dist"
if not (dist_dir / "index.html").is_file():
    raise SystemExit("缺少 frontend/dist，请先执行：cd frontend && npm run build")

playwright_datas, playwright_binaries, playwright_hiddenimports = collect_all("playwright")

a = Analysis(
    [str(ROOT / "backend" / "run.py")],
    pathex=[str(ROOT / "backend")],
    datas=[
        (str(dist_dir), "frontend/dist"),
        (str(ROOT / "bosun_skills"), "bosun_skills"),
        *collect_data_files("claude_agent_sdk"),
        *collect_data_files("certifi"),
        *playwright_datas,
    ],
    binaries=playwright_binaries,
    hiddenimports=[
        # uvicorn 的协议实现按配置字符串动态加载，静态图追不到
        *collect_submodules("uvicorn"),
        "websockets",
        *collect_submodules("claude_agent_sdk"),
        # harness 演进核心包：app 内经 try/except 双导入引用，静态图可能追不全
        *collect_submodules("harness_evolve"),
        *playwright_hiddenimports,
    ],
    excludes=["tkinter"],
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="bosun",
    console=True,
    upx=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name="bosun",
    upx=False,
)
