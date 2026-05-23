"""
api/reprocess.py
Повторная обработка проекта с другой моделью.

POST /api/projects/{id}/reprocess
  Body: { demucs_model, whisper_model, reset_transcription }
  → Отменяет активные jobs
  → Создаёт новую separation job с параметрами модели
  → Логирует PipelineEvent

GET /api/projects/{id}/events
  → История pipeline-событий (audit log)

GET /api/projects/{id}/transcriptions
  → Все версии транскрипций
  
POST /api/projects/{id}/transcriptions/{tr_id}/restore
  → Восстановить версию транскрипции
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from db.init_db import get_db
from db.models import Job, PipelineEvent, Project, Transcription

logger = logging.getLogger(__name__)
router = APIRouter()

# Доступные модели
DEMUCS_MODELS  = ["htdemucs", "htdemucs_ft", "mdx_extra", "htdemucs_6s"]
WHISPER_MODELS = ["tiny", "base", "small", "medium", "large"]


class ReprocessBody(BaseModel):
    demucs_model:         str  = "htdemucs"
    whisper_model:        str  = "base"
    reset_transcription:  bool = False   # True → удалить старые транскрипции


# ── POST /projects/{id}/reprocess ─────────────────────────────────────────────

@router.post("/projects/{project_id}/reprocess")
async def reprocess_project(
    project_id: str,
    body: ReprocessBody,
    db: AsyncSession = Depends(get_db),
):
    """
    Сбрасывает pipeline и запускает обработку заново с выбранными моделями.
    """
    if body.demucs_model not in DEMUCS_MODELS:
        raise HTTPException(status_code=400, detail=f"Неизвестная модель Demucs: {body.demucs_model}")
    if body.whisper_model not in WHISPER_MODELS:
        raise HTTPException(status_code=400, detail=f"Неизвестная модель Whisper: {body.whisper_model}")

    # Проверяем проект
    proj_res = await db.execute(select(Project).where(Project.id == project_id))
    project: Project | None = proj_res.scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="Проект не найден")

    # Отменяем все активные jobs
    jobs_res = await db.execute(
        select(Job).where(
            Job.project_id == project_id,
            Job.status.in_(["pending", "processing"]),
        )
    )
    cancelled_count = 0
    for job in jobs_res.scalars().all():
        job.status = "cancelled"
        cancelled_count += 1

    # Сбрасываем транскрипции если запрошено
    if body.reset_transcription:
        tr_res = await db.execute(
            select(Transcription).where(Transcription.project_id == project_id)
        )
        for tr in tr_res.scalars().all():
            tr.is_active = False

    # Сбрасываем проект
    project.status         = "processing"
    project.pipeline_state = "separating"

    # Создаём новую separation job с параметрами модели
    import time
    new_key = f"{project_id}:separation:reprocess:{int(time.time())}"
    new_job = Job(
        project_id      = project_id,
        type            = "separation",
        status          = "pending",
        idempotency_key = new_key,
        input_params    = json.dumps({
            "demucs_model":  body.demucs_model,
            "whisper_model": body.whisper_model,
            "reprocess":     True,
        }),
    )
    db.add(new_job)

    # Логируем событие
    db.add(PipelineEvent(
        project_id = project_id,
        event_type = "reprocess_requested",
        data_json  = json.dumps({
            "demucs_model":      body.demucs_model,
            "whisper_model":     body.whisper_model,
            "cancelled_jobs":    cancelled_count,
            "reset_transcription": body.reset_transcription,
        }),
    ))

    await db.commit()

    logger.info(
        "reprocess: project=%s demucs=%s whisper=%s cancelled=%d",
        project_id[:8], body.demucs_model, body.whisper_model, cancelled_count,
    )

    return {
        "status":          "reprocessing",
        "new_job_id":      new_job.id,
        "demucs_model":    body.demucs_model,
        "whisper_model":   body.whisper_model,
        "cancelled_jobs":  cancelled_count,
    }


# ── GET /projects/{id}/events ─────────────────────────────────────────────────

@router.get("/projects/{project_id}/events")
async def get_pipeline_events(
    project_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Audit log событий Pipeline для проекта."""
    res = await db.execute(
        select(PipelineEvent)
        .where(PipelineEvent.project_id == project_id)
        .order_by(PipelineEvent.created_at)
    )
    events = res.scalars().all()
    return [
        {
            "id":         e.id,
            "event_type": e.event_type,
            "data":       json.loads(e.data_json) if e.data_json else None,
            "created_at": e.created_at.isoformat(),
        }
        for e in events
    ]


# ── GET /projects/{id}/transcriptions ────────────────────────────────────────

@router.get("/projects/{project_id}/transcriptions")
async def list_transcriptions(
    project_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Все версии транскрипций проекта (история)."""
    res = await db.execute(
        select(Transcription)
        .where(Transcription.project_id == project_id)
        .options(selectinload(Transcription.lines))
        .order_by(Transcription.version.desc())
    )
    trs = res.scalars().all()
    return [
        {
            "transcription_id": tr.id,
            "version":          tr.version,
            "language":         tr.language,
            "confidence":       tr.confidence,
            "lines_count":      len(tr.lines),
            "is_active":        tr.is_active,
            "created_at":       tr.created_at.isoformat(),
        }
        for tr in trs
    ]


# ── POST /projects/{id}/transcriptions/{tr_id}/restore ───────────────────────

@router.post("/projects/{project_id}/transcriptions/{transcription_id}/restore")
async def restore_transcription(
    project_id: str,
    transcription_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Восстановить конкретную версию транскрипции как активную."""
    # Проверяем что указанная транскрипция существует и принадлежит проекту
    tr_res = await db.execute(
        select(Transcription).where(
            Transcription.id         == transcription_id,
            Transcription.project_id == project_id,
        )
    )
    target: Transcription | None = tr_res.scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=404, detail="Транскрипция не найдена")

    # Деактивируем все остальные
    all_res = await db.execute(
        select(Transcription).where(Transcription.project_id == project_id)
    )
    for tr in all_res.scalars().all():
        tr.is_active = tr.id == transcription_id

    # Логируем событие
    db.add(PipelineEvent(
        project_id = project_id,
        event_type = "transcription_restored",
        data_json  = json.dumps({
            "transcription_id": transcription_id,
            "version":          target.version,
        }),
    ))

    await db.commit()
    logger.info("restore_transcription: project=%s v%d", project_id[:8], target.version)

    return {
        "status":           "restored",
        "transcription_id": transcription_id,
        "version":          target.version,
    }


# ── GET /api/models ────────────────────────────────────────────────────────────

@router.get("/models")
async def get_available_models():
    """Список доступных моделей для UI."""
    return {
        "demucs": [
            {"id": "htdemucs",    "name": "htdemucs",    "desc": "Быстро, хорошо"},
            {"id": "htdemucs_ft", "name": "htdemucs_ft", "desc": "Медленнее, лучше"},
            {"id": "mdx_extra",   "name": "mdx_extra",   "desc": "Лучшее качество"},
            {"id": "htdemucs_6s", "name": "6 стемов",    "desc": "Drums/Bass/Guitar/..."},
        ],
        "whisper": [
            {"id": "tiny",   "name": "tiny",   "desc": "~1GB VRAM, быстро"},
            {"id": "base",   "name": "base",   "desc": "~1GB VRAM, баланс"},
            {"id": "small",  "name": "small",  "desc": "~2GB VRAM, хорошо"},
            {"id": "medium", "name": "medium", "desc": "~5GB VRAM, отлично"},
            {"id": "large",  "name": "large",  "desc": "~10GB VRAM, лучшее"},
        ],
    }
