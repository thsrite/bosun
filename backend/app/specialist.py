"""专项验收计划与实际 Git 变更的核对。仅新启用的运行使用。"""
import math
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
import re
import subprocess

from . import acceptance

AREAS = {"behavior", "ui", "db", "api", "integration", "perf", "lint"}
SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
GUIDES = (
    "代码单增加 baseline（开工基线完整 Git SHA）。每个 items 条目填写独立 id（字母数字、横线、下划线）、"
    "area（behavior/ui/db/api/integration/perf/lint）、case（JUnit testcase 的 name 或 classname::name）；"
    "质量另选用例可填写 qa_case。每个编号只验证一种承诺。ui 视觉项写 reference（原型版本、视口、字体、状态）"
    "及 reference_sha256（已认可参照 PNG 的摘要），max_diff_ratio 为可接受的不同像素比例，默认零。无视觉参照的管理页按认可组件行为测。"
    "db 在 invariant 写数据归属、唯一性或实际事务约束；api 在 caller 写真实调用方并约定返回与副作用；integration 在 entry 写用户入口。"
    "perf 填 workload（固定样本及负载）和 budget={metric,unit,max}，来自项目预算；没有预算先做基线调查。"
    '查项目已有慢路径和联调边界，适用时登记 impact_paths，例如 {"perf":["backend/modules/reports/export.py"]}。'
    "候选触及登记路径时复核该专项。依据实际影响选项，不要求每单全测。",
    "以 git diff baseline..候选核对页面、存储、路由和调用方；热查询、导出、反馈卡顿另核对性能。"
    "漏项回需求补单。仅命中文件但实际不影响此类行为时，在本角色 JSON 的 scope_notes 中按 area 记具体理由。"
    "开发和质量分别核对。数据库迁移用目标方言及旧记录升级、另连接读回；API 走真实路由并核对持久化和旧调用。"
    "有原型的 UI 视觉和点击行为分两项。性能用同负载基线与候选对照。"
    "继续用 run_delivery_check.py。测试进程拿到 BOSUN_REPORT（JUnit XML 绝对路径）和 BOSUN_EVIDENCE_DIR。"
    "pytest 用 --junitxml={report}，Playwright 用 --reporter=junit（输出位置已注入）。其他测试工具可用适配器生成 JUnit。"
    "ui 在证据目录写 编号.reference.png 和 编号.actual.png，使用装有 Pillow 的远端 python3；可用 --collector-python 指定其路径。"
    "db/api/integration 在 编号.json 写 observed 实际读回；db 加 engine，api 加 request 和 HTTP status，integration 加 record 业务记录标识。"
    "perf 的 编号.json 写 metric、unit、baseline、candidate、load（同任务单 workload）。"
    "报告只列实际执行的断言；用脱敏样本，回执归档后清理远端。未测先在本轮补跑或修正报告；"
    "确实缺环境或产品口径时才等待输入。实现或预算断言失败交开发返工。",
)


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def validate(spec):
    if not SHA.fullmatch(acceptance.required(spec, "baseline")):
        raise ValueError("baseline 需要开工基线完整 SHA")
    used = [set(), set()]
    watches = spec.get("impact_paths", {})
    if not isinstance(watches, dict) or any(
        area not in AREAS or not isinstance(patterns, list)
        or any(not isinstance(p, str) or not p.strip() for p in patterns)
        for area, patterns in watches.items()
    ):
        raise ValueError("impact_paths 应按专项列出项目相对路径或匹配模式")
    for key, item in acceptance.plan(spec).items():
        if not re.fullmatch(r"[A-Za-z0-9_-]+", key) or item.get("area") not in AREAS:
            raise ValueError(f"{key} 需要有效的独立编号与专项类别")
        name = acceptance.required(item, "case")
        for area, field in (("db", "invariant"), ("api", "caller"), ("integration", "entry"), ("perf", "workload")):
            if item["area"] == area:
                acceptance.required(item, field)
        qa = acceptance.required(item, "qa_case") if "qa_case" in item else name
        for names, selector in zip(used, (name, qa)):
            if any(selector == old or (selector.rsplit("::", 1)[-1] == old.rsplit("::", 1)[-1]
                                       and ("::" not in selector or "::" not in old)) for old in names):
                raise ValueError(f"重复用例 {selector}，每个角色的检查需分别取证")
            names.add(selector)
        if item["area"] == "ui":
            acceptance.required(item, "reference")
            ratio = item.get("max_diff_ratio", 0)
            if not number(ratio) or not 0 <= ratio < 1:
                raise ValueError(f"{key} 像素差异比例须大于等于零且小于一")
            if not re.fullmatch(r"[0-9a-f]{64}", acceptance.required(item, "reference_sha256")):
                raise ValueError(f"{key} 缺少已认可参照图的 SHA256")
        if item["area"] == "perf":
            budget = item.get("budget")
            if not isinstance(budget, dict) or not number(budget.get("max")):
                raise ValueError(f"{key} 缺少性能上限")
            for field in ("metric", "unit"):
                acceptance.required(budget, field)


def area_for(path):
    p = PurePosixPath(path.lower())
    if (any(x in p.parts for x in ("tests", "tests_db", "tests_unit", "e2e", "docs", "__tests__"))
            or p.suffix == ".md" or p.name.startswith("test_") or p.stem.endswith((".spec", ".test"))):
        return None
    if p.suffix in {".vue", ".tsx", ".jsx", ".css", ".scss", ".html", ".svelte"}:
        return "ui"
    if p.suffix in {".py", ".sql", ".ts", ".js"}:
        if p.suffix == ".sql" or set(p.parts) & {"alembic", "migrations", "models"} or p.stem in {"models", "schema_sql", "db"}:
            return "db"
        if set(p.parts) & {"routers", "routes"} or p.stem in {"router", "routes", "api"}:
            return "api"
    return None


def git(project, *args):
    try:
        return subprocess.run(["git", *args], cwd=project, capture_output=True)
    except OSError as exc:
        raise ValueError("无法访问项目仓库以核对变更") from exc


def check_scope(project, spec, candidate, report):
    if not SHA.fullmatch(candidate):
        raise ValueError("专项核对需要完整候选 SHA")
    base = spec["baseline"]
    if git(project, "merge-base", "--is-ancestor", base, candidate).returncode:
        raise ValueError("候选提交须包含任务单登记的开工基线")
    diff = git(project, "diff", "--name-only", "--no-renames", "-z", base, candidate)
    if diff.returncode:
        raise ValueError("无法读取基线与候选提交之间的实际差异")
    planned = {item["area"] for item in spec["items"]}
    notes = report.get("scope_notes", {})
    if not isinstance(notes, dict):
        raise ValueError("scope_notes 应按专项类别填写不适用理由")
    missing = {}
    for path in filter(None, diff.stdout.decode(errors="replace").split("\0")):
        areas = {area for area, patterns in spec.get("impact_paths", {}).items()
                 if any(fnmatchcase(path, pattern) for pattern in patterns)}
        areas.add(area_for(path))
        for area in areas - planned - {None}:
            if not (isinstance(notes.get(area), str) and notes[area].strip()):
                missing.setdefault(area, path)
    if missing:
        raise ValueError("实际变更须补专项验收或说明不适用理由：" + "、".join(f"{a}（{p}）" for a, p in missing.items()))
