"""统计面板数据：兼容旧接口 + 统一诊断 dashboard。"""
from __future__ import annotations

from fastapi import APIRouter

from ..services import stats as stats_service

router = APIRouter(prefix="/api/stats", tags=["stats"])


@router.get("/dashboard")
def dashboard(days: int = 30):
    return stats_service.build_dashboard(days)


@router.get("/host")
def host():
    return stats_service.host_metrics()


@router.get("/overview")
def overview():
    summary = stats_service.build_dashboard()["summary"]
    return {
        "total_tasks": summary["total_tasks"],
        "by_status": summary["by_status"],
        "success_rate": summary["success_rate"],
        "avg_duration_sec": summary["avg_duration_sec"],
        "projects": summary["projects"],
        "deleted_tasks": summary["deleted_tasks"],
    }


@router.get("/engines")
def engines():
    return {
        r["engine"]: r["tasks"]
        for r in stats_service.build_dashboard()["engine_quality"]
    }


@router.get("/findings")
def findings():
    h = stats_service.build_dashboard()["finding_health"]
    return {"by_source": h["by_source"], "by_severity": h["by_severity"], "by_status": h["by_status"]}


@router.get("/timeline")
def timeline(days: int = 14):
    return stats_service.build_dashboard(days)["throughput"]


@router.get("/tokens")
def tokens():
    """token 消耗：按项目排行(任务+自愈) + 按引擎分布 + 总计。"""
    t = stats_service.build_dashboard()["token_economics"]
    return {"by_project": t["by_project"], "by_engine": t["by_engine"], "total": t["total"]}


@router.get("/tokens-timeline")
def tokens_timeline(days: int = 14):
    """按天的 token 消耗(任务 + 自愈)，用结束时间归日。"""
    return stats_service.build_dashboard(days)["token_economics"]["timeline"]


@router.get("/activity")
def activity():
    return stats_service.build_dashboard()["project_activity"]
