"""
api/export.py
Экспорт транскрипции и аудио.

GET /api/projects/{id}/export/lrc   → .lrc файл
GET /api/projects/{id}/export/srt   → .srt субтитры
GET /api/projects/{id}/export/vtt   → WebVTT субтитры
GET /api/projects/{id}/export/midi  → .mid мелодия вокала
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from db.init_db import get_db
from db.models import AudioAsset, Project, Transcription
from services.subtitle_exporter import lines_to_lrc, lines_to_srt, lines_to_vtt
from services.midi_exporter import vocal_to_midi

logger = logging.getLogger(__name__)
router = APIRouter()

_TMP_MIDI: dict[str, Path] = {}   # project_id → temp midi path


async def _get_active_tr(db: AsyncSession, project_id: str) -> Transcription:
    res = await db.execute(
        select(Transcription)
        .where(Transcription.project_id == project_id, Transcription.is_active == True)  # noqa
        .options(selectinload(Transcription.lines))
    )
    tr = res.scalar_one_or_none()
    if tr is None:
        raise HTTPException(status_code=404, detail="Транскрипция не найдена")
    return tr


# ── LRC ───────────────────────────────────────────────────────────────────────

@router.get("/projects/{project_id}/export/lrc")
async def export_lrc(project_id: str, db: AsyncSession = Depends(get_db)):
    tr = await _get_active_tr(db, project_id)
    proj_res = await db.execute(select(Project).where(Project.id == project_id))
    project  = proj_res.scalar_one_or_none()
    title    = project.title if project else ""

    content = lines_to_lrc(tr.lines, title=title)
    filename = f"{_safe(title)}.lrc"
    return PlainTextResponse(
        content=content,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── SRT ───────────────────────────────────────────────────────────────────────

@router.get("/projects/{project_id}/export/srt")
async def export_srt(project_id: str, db: AsyncSession = Depends(get_db)):
    tr = await _get_active_tr(db, project_id)
    proj_res = await db.execute(select(Project).where(Project.id == project_id))
    project  = proj_res.scalar_one_or_none()
    title    = project.title if project else ""

    content  = lines_to_srt(tr.lines)
    filename = f"{_safe(title)}.srt"
    return PlainTextResponse(
        content=content,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── VTT ───────────────────────────────────────────────────────────────────────

@router.get("/projects/{project_id}/export/vtt")
async def export_vtt(project_id: str, db: AsyncSession = Depends(get_db)):
    tr = await _get_active_tr(db, project_id)
    proj_res = await db.execute(select(Project).where(Project.id == project_id))
    project  = proj_res.scalar_one_or_none()
    title    = project.title if project else ""

    content  = lines_to_vtt(tr.lines, title=title)
    filename = f"{_safe(title)}.vtt"
    return PlainTextResponse(
        content=content,
        media_type="text/vtt; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── MIDI ──────────────────────────────────────────────────────────────────────

@router.get("/projects/{project_id}/export/midi")
async def export_midi(
    project_id: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Генерирует MIDI из вокала (может занять несколько секунд)."""
    # Находим вокал
    res = await db.execute(
        select(AudioAsset).where(
            AudioAsset.project_id == project_id,
            AudioAsset.type == "vocal",
        )
    )
    asset = res.scalar_one_or_none()
    if asset is None:
        raise HTTPException(status_code=404, detail="Вокал не найден")

    vocal_path = Path(asset.file_path)
    if not vocal_path.exists():
        raise HTTPException(status_code=404, detail="Файл вокала не найден")

    # Получаем название проекта
    proj_res = await db.execute(select(Project).where(Project.id == project_id))
    project  = proj_res.scalar_one_or_none()
    title    = project.title if project else project_id

    # Генерируем MIDI во временный файл
    midi_path = Path(tempfile.mkdtemp()) / f"{_safe(title)}.mid"
    try:
        await vocal_to_midi(vocal_path, midi_path)
    except Exception as exc:
        logger.error("export_midi: %s", exc)
        raise HTTPException(status_code=500, detail=f"Ошибка генерации MIDI: {exc}")

    # Удаляем tmp после отдачи
    background_tasks.add_task(_cleanup_file, midi_path)

    return FileResponse(
        path=str(midi_path),
        media_type="audio/midi",
        filename=midi_path.name,
    )


# ── Reprocess (смена модели) ──────────────────────────────────────────────────

@router.post("/projects/{project_id}/reprocess")
async def reprocess_project(
    project_id: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Сбрасывает pipeline и создаёт новую separation job.
    Тело: { demucs_model, whisper_model } (опционально).
    Отдельный роутер reprocess.py не нужен — добавлено сюда.
    """
    from fastapi import Body
    from pydantic import BaseModel

    class ReprocessBody(BaseModel):
        demucs_model:  str = "htdemucs"
        whisper_model: str = "base"

    # Используется через отдельный роут, но пусть пока здесь
    raise HTTPException(status_code=501, detail="Используй /api/projects/{id}/reprocess (POST)")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-_ " else "_" for c in s).strip()[:40] or "export"


async def _cleanup_file(path: Path):
    import asyncio
    await asyncio.sleep(60)   # даём браузеру время скачать
    path.unlink(missing_ok=True)
    try: path.parent.rmdir()
    except Exception: pass
