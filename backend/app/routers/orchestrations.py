"""可配置编排模板与运行 API。"""
from __future__ import annotations

import io
import mimetypes
from pathlib import PurePosixPath
import time
import zipfile

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from .. import db, orchestrations, uploads, workflow_policy

router = APIRouter(tags=["orchestrations"])


class StepBody(BaseModel):
    name: str
    engine: str
    model: str = ""
    reasoning_effort: str = ""
    role_prompt: str


class TemplateBody(BaseModel):
    name: str
    enabled: bool = True
    steps: list[StepBody]


class AcceptanceSetting(BaseModel):
    enabled: bool
    remote_checks: bool | None = None
    specialist_checks: bool | None = None


class RunBody(BaseModel):
    orchestration_id: int
    project_id: int
    prompt: str
    title: str | None = None
    priority: int = 5
    auto_approve: bool = False
    start: bool = False
    on_demand: bool = True


class SignoffBody(BaseModel):
    decision: str
    signer: str = "产品负责人"
    comment: str = ""


class WorkflowPolicyBody(BaseModel):
    trust_level: int | None = None
    diff_integrity_gate: bool | None = None
    require_product_signoff: bool | None = None
    auto_merge_globs: list[str] | None = None


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except orchestrations.OrchestrationError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc


@router.get("/api/orchestrations")
def list_templates():
    return orchestrations.list_templates()


@router.post("/api/orchestrations")
def create_template(body: TemplateBody):
    return _call(
        orchestrations.create_template,
        body.name,
        [step.model_dump() for step in body.steps],
        body.enabled,
    )


@router.put("/api/orchestrations/{template_id}")
def update_template(template_id: int, body: TemplateBody):
    return _call(
        orchestrations.update_template,
        template_id,
        body.name,
        [step.model_dump() for step in body.steps],
        body.enabled,
    )


@router.delete("/api/orchestrations/{template_id}")
def delete_template(template_id: int):
    _call(orchestrations.delete_template, template_id)
    return {"ok": True}


@router.put("/api/orchestrations/{template_id}/acceptance")
def configure_acceptance(template_id: int, body: AcceptanceSetting):
    with db._lock:
        template = _call(orchestrations.get_template, template_id)
        if body.enabled and len(template["steps"]) != 3:
            raise HTTPException(409, "验收表适用需求、开发、质量三角色模板")
        db.execute("UPDATE orchestration SET acceptance_gate=?, remote_checks=COALESCE(?,remote_checks), "
                   "specialist_checks=COALESCE(?,specialist_checks), updated_at=? WHERE id=?",
                   (int(body.enabled), body.remote_checks, body.specialist_checks, time.time(), template_id))
    return orchestrations.get_template(template_id)


@router.get("/api/orchestration-runs")
def list_runs(project_id: int | None = None, show_hidden: bool = False):
    runs = orchestrations.list_runs(project_id)
    if show_hidden:
        return runs
    visible_ids = {r["id"] for r in db.query("SELECT id FROM project WHERE hidden=0")}
    return [run for run in runs if run["project_id"] in visible_ids]


@router.post("/api/orchestration-runs")
def create_run(body: RunBody):
    return _call(
        orchestrations.create_run,
        body.orchestration_id,
        body.project_id,
        body.prompt,
        body.title,
        body.priority,
        body.auto_approve,
        body.start,
        on_demand=body.on_demand,
    )


@router.get("/api/orchestration-runs/{run_id}")
def get_run(run_id: int):
    return _call(orchestrations.get_run, run_id)


@router.post("/api/orchestration-runs/{run_id}/start")
def start_run(run_id: int):
    return _call(orchestrations.start_run, run_id)


@router.post("/api/orchestration-runs/{run_id}/cancel")
def cancel_run(run_id: int):
    return _call(orchestrations.cancel_run, run_id)


@router.post("/api/orchestration-runs/{run_id}/resume")
def resume_run(run_id: int):
    """重新拉起掉线的角色（后端重启后班组不会自动重开，由用户确认）。"""
    return _call(orchestrations.resume_run, run_id)


@router.get("/api/orchestration-runs/{run_id}/messages")
def list_messages(run_id: int):
    _call(orchestrations.get_run, run_id)
    return _call(orchestrations.list_messages, run_id)


@router.get("/api/orchestration-runs/{run_id}/trace")
def trace_run(run_id: int):
    return _call(orchestrations.run_trace, run_id)


@router.get("/api/orchestration-runs/{run_id}/receipts/{receipt}/archive")
def download_report(run_id: int, receipt: int):
    row = db.query_one("SELECT r.archive FROM delivery_report r JOIN delivery_check c ON c.id=r.receipt "
                       "JOIN orchestration_step_run s ON s.id=c.step_id WHERE s.run_id=? AND r.receipt=?",
                       (run_id, receipt))
    if row is None:
        raise HTTPException(404, "本次运行没有这份证据包")
    return Response(row[0], media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="receipt-{receipt}.zip"', "Cache-Control": "no-store",
    })


def _load_receipt_archive(run_id: int, receipt: int) -> bytes:
    row = db.query_one(
        "SELECT r.archive FROM delivery_report r JOIN delivery_check c ON c.id=r.receipt "
        "JOIN orchestration_step_run s ON s.id=c.step_id WHERE s.run_id=? AND r.receipt=?",
        (run_id, receipt),
    )
    if row is None:
        raise HTTPException(404, "本次运行没有这份证据包")
    return bytes(row[0])


@router.get("/api/orchestration-runs/{run_id}/receipts/{receipt}/files")
def list_receipt_files(run_id: int, receipt: int):
    raw = _load_receipt_archive(run_id, receipt)
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            items = []
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = info.filename
                suffix = PurePosixPath(name).suffix.lower()
                kind = "image" if suffix in (".png", ".jpg", ".jpeg", ".webp") else (
                    "json" if suffix == ".json" else "text"
                )
                items.append({"name": name, "size": info.file_size, "kind": kind})
    except zipfile.BadZipFile as exc:
        raise HTTPException(500, "证据包已损坏") from exc
    return {"receipt": receipt, "files": items}


@router.get("/api/orchestration-runs/{run_id}/receipts/{receipt}/file/{file_path:path}")
def read_receipt_file(run_id: int, receipt: int, file_path: str):
    norm = PurePosixPath(file_path)
    if norm.is_absolute() or ".." in norm.parts:
        raise HTTPException(400, "非法文件路径")
    raw = _load_receipt_archive(run_id, receipt)
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            target = str(norm)
            if target not in zf.namelist():
                raise HTTPException(404, "证据包中不存在该文件")
            data = zf.read(target)
    except zipfile.BadZipFile as exc:
        raise HTTPException(500, "证据包已损坏") from exc
    media_type, _ = mimetypes.guess_type(target)
    return Response(
        data,
        media_type=media_type or "application/octet-stream",
        headers={"Cache-Control": "no-store"},
    )


@router.post("/api/orchestration-runs/{run_id}/signoff")
def signoff_run(run_id: int, body: SignoffBody):
    return _call(orchestrations.record_signoff, run_id, body.decision, body.signer, body.comment)


@router.get("/api/projects/{project_id}/workflow-policy")
def get_workflow_policy(project_id: int):
    if db.query_one("SELECT id FROM project WHERE id=?", (project_id,)) is None:
        raise HTTPException(404, "项目不存在")
    return workflow_policy.get_project_policy(project_id)


@router.put("/api/projects/{project_id}/workflow-policy")
def put_workflow_policy(project_id: int, body: WorkflowPolicyBody):
    if db.query_one("SELECT id FROM project WHERE id=?", (project_id,)) is None:
        raise HTTPException(404, "项目不存在")
    try:
        return workflow_policy.update_project_policy(
            project_id,
            trust_level=body.trust_level,
            diff_integrity_gate=body.diff_integrity_gate,
            require_product_signoff=body.require_product_signoff,
            auto_merge_globs=body.auto_merge_globs,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/api/orchestration-runs/{run_id}/upload-file")
async def upload_run_file(run_id: int, file: UploadFile = File(...)):
    run = _call(orchestrations.get_run, run_id)
    if run["status"] != "draft":
        raise HTTPException(409, "只有待办编排可以追加附件")
    project = db.query_one("SELECT path FROM project WHERE id=?", (run["project_id"],))
    if project is None:
        raise HTTPException(404, "项目不存在")
    data = await file.read(uploads.MAX_UPLOAD_BYTES + 1)
    try:
        path = uploads.save_project_upload(project["path"], file.filename or "file", data)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except OverflowError as exc:
        raise HTTPException(413, str(exc)) from exc
    separator = "\n\n附件（本地路径，请查看）：\n" if "附件（本地路径，请查看）：" not in run["original_prompt"] else "\n"
    db.execute(
        "UPDATE orchestration_run SET original_prompt=original_prompt||?||?,updated_at=? WHERE id=? AND status='draft'",
        (separator, f"- {path}", time.time(), run_id),
    )
    return {"path": path}
