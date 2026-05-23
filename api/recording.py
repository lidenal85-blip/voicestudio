"""
api/recording.py
Эндпоинты записи пользователя.

POST /api/projects/{project_id}/recordings  — загрузить mic-запись, запустить микс
GET  /api/projects/{project_id}/recordings  — список записей проекта
GET  /api/recordings/{recording_id}         — детали + URL микса
DELETE /api/recordings/{recording_id}       — удалить запись и файлы
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, UploadFile, File, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.init_db import get_db
from db.models import AudioAsset, Project, Recording
from services.converter import get_duration
from services.mixer import mix_audio
from services.converter import convert_to_wav
from services.storage import LocalStorageAdapter, get_storage

logger = logging.getLogger(__name__)
router = APIRouter()


# ── POST /projects/{id}/recordings ────────────────────────────────────────────

@router.post("/projects/{project_id}/recordings", status_code=status.HTTP_201_CREATED)
async def create_recording(
    project_id: str,
    background_tasks: BackgroundTasks,
    mic_audio: UploadFile = File(..., description="audio/webm или audio/wav из браузера"),
    timing_offset_ms: int = Form(default=0, description="Измеренная latency в мс"),
    duration_ms: int = Form(default=0),
    db: AsyncSession = Depends(get_db),
    storage: LocalStorageAdapter = Depends(get_storage),
):
    """
    Принимает mic-запись из браузера (WebM/Opus или WAV).
    Конвертирует в WAV, запускает FFmpeg-микс с инструменталом в фоне.
    """
    # Проверяем проект
    proj_res = await db.execute(select(Project).where(Project.id == project_id))
    project: Project | None = proj_res.scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="Проект не найден")

    # Находим инструментал
    inst_res = await db.execute(
        select(AudioAsset).where(
            AudioAsset.project_id == project_id,
            AudioAsset.type       == "instrumental",
        )
    )
    instrumental_asset: AudioAsset | None = inst_res.scalar_one_or_none()
    if instrumental_asset is None:
        raise HTTPException(status_code=404, detail="Инструментал ещё не готов")

    instrumental_path = Path(instrumental_asset.file_path)
    if not instrumental_path.exists():
        raise HTTPException(status_code=404, detail="Файл инструментала не найден")

    # Определяем расширение файла
    content_type = mic_audio.content_type or ""
    raw_ext = "webm" if "webm" in content_type else "wav"
    raw_filename = f"mic_raw.{raw_ext}"

    # Сохраняем raw mic
    rec_dir = storage._project_dir(project_id) / "recordings"
    rec_dir.mkdir(exist_ok=True)

    raw_path = rec_dir / raw_filename
    content  = await mic_audio.read()
    raw_path.write_bytes(content)

    # Конвертируем в WAV если нужно
    if raw_ext != "wav":
        mic_wav_path = rec_dir / "mic.wav"
        try:
            await convert_to_wav(raw_path, mic_wav_path)
            raw_path.unlink(missing_ok=True)
        except RuntimeError as exc:
            raw_path.unlink(missing_ok=True)
            raise HTTPException(status_code=500, detail=f"Конвертация: {exc}")
    else:
        mic_wav_path = raw_path

    # Считаем длительность
    actual_duration = await get_duration(mic_wav_path)
    if actual_duration == 0 and duration_ms > 0:
        actual_duration = duration_ms

    # Создаём запись в БД
    rec = Recording(
        project_id       = project_id,
        mic_audio_path   = str(mic_wav_path),
        timing_offset_ms = timing_offset_ms,
        duration_ms      = actual_duration,
        size_bytes_mic   = mic_wav_path.stat().st_size,
        status           = "processing",
    )
    db.add(rec)
    await db.commit()

    rec_id = rec.id
    mixed_path = rec_dir / f"mixed_{rec_id[:8]}.wav"

    # Запускаем микширование в фоне
    background_tasks.add_task(
        _mix_background,
        rec_id             = rec_id,
        instrumental_path  = instrumental_path,
        mic_path           = mic_wav_path,
        output_path        = mixed_path,
        timing_offset_ms   = timing_offset_ms,
    )

    logger.info("create_recording: project=%s rec=%s offset=%dms", project_id, rec_id[:8], timing_offset_ms)

    return {
        "recording_id":    rec_id,
        "status":          "processing",
        "timing_offset_ms": timing_offset_ms,
        "duration_ms":     actual_duration,
        "message":         "Микширование запущено, проверь статус через GET /api/recordings/{id}",
    }


async def _mix_background(
    rec_id: str,
    instrumental_path: Path,
    mic_path: Path,
    output_path: Path,
    timing_offset_ms: int,
) -> None:
    from db.init_db import _get_session_factory
    from sqlalchemy import select

    try:
        await mix_audio(
            instrumental_path = instrumental_path,
            mic_path          = mic_path,
            output_path       = output_path,
            timing_offset_ms  = timing_offset_ms,
        )
        duration_ms = await get_duration(output_path)

        async with _get_session_factory()() as s:
            res = await s.execute(select(Recording).where(Recording.id == rec_id))
            rec = res.scalar_one_or_none()
            if rec:
                rec.mixed_audio_path  = str(output_path)
                rec.size_bytes_mixed  = output_path.stat().st_size
                rec.duration_ms       = duration_ms
                rec.status            = "ready"
            await s.commit()

        logger.info("_mix_background: rec=%s DONE mixed=%.1fMB", rec_id[:8], output_path.stat().st_size / 1_048_576)

    except Exception as exc:
        logger.error("_mix_background: rec=%s ERROR: %s", rec_id[:8], exc)
        async with _get_session_factory()() as s:
            res = await s.execute(select(Recording).where(Recording.id == rec_id))
            rec = res.scalar_one_or_none()
            if rec:
                rec.status    = "failed"
                rec.error_msg = str(exc)
            await s.commit()


# ── GET /projects/{id}/recordings ─────────────────────────────────────────────

@router.get("/projects/{project_id}/recordings")
async def list_recordings(
    project_id: str,
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(
        select(Recording)
        .where(Recording.project_id == project_id)
        .order_by(Recording.created_at.desc())
    )
    recs = res.scalars().all()

    return [
        {
            "recording_id":    r.id,
            "status":          r.status,
            "timing_offset_ms": r.timing_offset_ms,
            "duration_ms":     r.duration_ms,
            "size_bytes_mic":  r.size_bytes_mic,
            "size_bytes_mixed": r.size_bytes_mixed,
            "created_at":      r.created_at.isoformat(),
            "has_mix":         r.mixed_audio_path is not None,
        }
        for r in recs
    ]


# ── GET /recordings/{id} ──────────────────────────────────────────────────────

@router.get("/recordings/{recording_id}")
async def get_recording(
    recording_id: str,
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(Recording).where(Recording.id == recording_id))
    rec: Recording | None = res.scalar_one_or_none()
    if rec is None:
        raise HTTPException(status_code=404, detail="Запись не найдена")

    return {
        "recording_id":    rec.id,
        "project_id":      rec.project_id,
        "status":          rec.status,
        "timing_offset_ms": rec.timing_offset_ms,
        "duration_ms":     rec.duration_ms,
        "size_bytes_mic":  rec.size_bytes_mic,
        "size_bytes_mixed": rec.size_bytes_mixed,
        "created_at":      rec.created_at.isoformat(),
        "error_msg":       rec.error_msg,
        "mic_url":   f"/api/recordings/{rec.id}/mic"   if rec.mic_audio_path   else None,
        "mixed_url": f"/api/recordings/{rec.id}/mixed" if rec.mixed_audio_path else None,
    }


# ── GET /recordings/{id}/mic|mixed ────────────────────────────────────────────

@router.get("/recordings/{recording_id}/mic")
async def get_recording_mic(recording_id: str, db: AsyncSession = Depends(get_db)):
    return await _serve_recording_file(recording_id, "mic", db)


@router.get("/recordings/{recording_id}/mixed")
async def get_recording_mixed(recording_id: str, db: AsyncSession = Depends(get_db)):
    return await _serve_recording_file(recording_id, "mixed", db)


async def _serve_recording_file(recording_id: str, file_type: str, db: AsyncSession):
    res = await db.execute(select(Recording).where(Recording.id == recording_id))
    rec: Recording | None = res.scalar_one_or_none()
    if rec is None:
        raise HTTPException(status_code=404, detail="Запись не найдена")

    path_str = rec.mic_audio_path if file_type == "mic" else rec.mixed_audio_path
    if not path_str:
        raise HTTPException(status_code=404, detail="Файл ещё не готов")

    fpath = Path(path_str)
    if not fpath.exists():
        raise HTTPException(status_code=404, detail="Файл не найден на диске")

    return FileResponse(path=str(fpath), media_type="audio/wav", filename=fpath.name)


# ── DELETE /recordings/{id} ───────────────────────────────────────────────────

@router.delete("/recordings/{recording_id}")
async def delete_recording(recording_id: str, db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(Recording).where(Recording.id == recording_id))
    rec: Recording | None = res.scalar_one_or_none()
    if rec is None:
        raise HTTPException(status_code=404, detail="Запись не найдена")

    for path_str in (rec.mic_audio_path, rec.mixed_audio_path):
        if path_str:
            Path(path_str).unlink(missing_ok=True)

    await db.delete(rec)
    logger.info("delete_recording: %s", recording_id[:8])
    return {"status": "deleted"}
