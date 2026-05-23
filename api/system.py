"""
api/system.py
Health check и метрики системы.

GET /health          — 200 OK / 503 если DB недоступна
GET /api/metrics     — JSON-метрики (uptime, jobs, storage, scheduler)
GET /api/storage/stats — использование хранилища по проектам
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import Settings, get_settings
from db.init_db import get_db
from db.models import Job, Project, Recording
from infrastructure.scheduler import metrics

router = APIRouter(tags=["system"])


# ── GET /health ────────────────────────────────────────────────────────────────

@router.get("/health")
async def health_check(db: AsyncSession = Depends(get_db)):
    """Readiness probe. 200 = всё ок, 503 = БД недоступна."""
    try:
        await db.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False

    status = "ok" if db_ok else "degraded"
    code   = 200  if db_ok else 503

    return JSONResponse(
        status_code=code,
        content={
            "status":     status,
            "timestamp":  datetime.now(timezone.utc).isoformat(),
            "db":         "ok" if db_ok else "error",
            "uptime_sec": int(time.time() - metrics.started_at),
        },
    )


# ── GET /api/metrics ───────────────────────────────────────────────────────────

@router.get("/api/metrics")
async def get_metrics(
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Метрики системы в JSON-формате."""

    # Jobs статистика
    jobs_res = await db.execute(
        select(Job.status, func.count(Job.id)).group_by(Job.status)
    )
    jobs_by_status = dict(jobs_res.all())

    # Projects статистика
    projects_res = await db.execute(
        select(Project.status, func.count(Project.id)).group_by(Project.status)
    )
    projects_by_status = dict(projects_res.all())

    # Recordings
    recordings_res = await db.execute(
        select(func.count(Recording.id), func.sum(Recording.size_bytes_mixed))
    )
    rec_count, rec_bytes = recordings_res.one()

    # Uptime
    uptime_sec = int(time.time() - metrics.started_at)
    uptime_str = _fmt_uptime(uptime_sec)

    # Disk free
    disk_free_mb = _disk_free_mb(settings.STORAGE_PATH)

    return {
        "uptime_sec":   uptime_sec,
        "uptime":       uptime_str,
        "timestamp":    datetime.now(timezone.utc).isoformat(),

        "storage": {
            "used_bytes":  metrics.storage_bytes_used,
            "used_mb":     round(metrics.storage_bytes_used / 1_048_576, 1),
            "free_mb":     disk_free_mb,
            "path":        str(settings.STORAGE_PATH),
        },

        "jobs": {
            "by_status":    jobs_by_status,
            "pending":      jobs_by_status.get("pending", 0),
            "processing":   jobs_by_status.get("processing", 0),
            "done":         jobs_by_status.get("done", 0),
            "failed":       jobs_by_status.get("failed", 0),
        },

        "projects": {
            "by_status":    projects_by_status,
            "total":        sum(projects_by_status.values()),
            "ready":        projects_by_status.get("ready", 0),
            "processing":   projects_by_status.get("processing", 0),
            "failed":       projects_by_status.get("failed", 0),
        },

        "recordings": {
            "count":         rec_count or 0,
            "total_mb":      round((rec_bytes or 0) / 1_048_576, 1),
        },

        "scheduler": {
            "jobs_timed_out":     metrics.jobs_timed_out,
            "jobs_recovered":     metrics.jobs_recovered,
            "temp_files_cleaned": metrics.temp_files_cleaned,
            "last_scan_at":       metrics.last_scan_at.isoformat() if metrics.last_scan_at else None,
            "last_cleanup_at":    metrics.last_cleanup_at.isoformat() if metrics.last_cleanup_at else None,
        },
    }


# ── GET /api/storage/stats ────────────────────────────────────────────────────

@router.get("/api/storage/stats")
async def storage_stats(
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Использование хранилища по проектам."""
    projects_res = await db.execute(
        select(Project.id, Project.title, Project.status, Project.created_at)
        .order_by(Project.created_at.desc())
    )
    projects = projects_res.all()

    project_sizes = []
    for p_id, p_title, p_status, p_created in projects:
        proj_dir = settings.STORAGE_PATH / p_id
        size_bytes = _dir_size(proj_dir) if proj_dir.exists() else 0
        project_sizes.append({
            "project_id":  p_id,
            "title":       p_title,
            "status":      p_status,
            "size_bytes":  size_bytes,
            "size_mb":     round(size_bytes / 1_048_576, 1),
            "created_at":  p_created.isoformat(),
        })

    total_bytes = sum(p["size_bytes"] for p in project_sizes)

    return {
        "total_bytes":  total_bytes,
        "total_mb":     round(total_bytes / 1_048_576, 1),
        "free_mb":      _disk_free_mb(settings.STORAGE_PATH),
        "projects":     project_sizes,
    }


# ── Helpers ────────────────────────────────────────────────────────────────────

def _fmt_uptime(sec: int) -> str:
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    if d: return f"{d}d {h}h {m}m"
    if h: return f"{h}h {m}m"
    return f"{m}m {s}s"


def _disk_free_mb(path: Path) -> float:
    try:
        stat = os.statvfs(path)
        return round(stat.f_bavail * stat.f_frsize / 1_048_576, 1)
    except Exception:
        return -1.0


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for f in path.rglob("*"):
            if f.is_file():
                try: total += f.stat().st_size
                except OSError: pass
    except Exception: pass
    return total
