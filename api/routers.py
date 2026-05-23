"""
api/routers.py — Core API endpoints.
Fixes:
  - BUG-1: idempotency_key теперь timestamp-based (не :v1)
  - BUG-7: входящие MP3/FLAC конвертируются в WAV после приёма
"""
from __future__ import annotations
import json, logging, time as _time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import Settings, get_settings
from db.init_db import get_db
from db.models import AudioAsset, Job, Project
from services.converter import convert_to_wav, get_duration
from services.storage import LocalStorageAdapter, get_storage

logger = logging.getLogger(__name__)
router = APIRouter()
ALLOWED_EXTENSIONS = {"mp3", "wav", "flac", "m4a"}

def _check_secret(secret: str, settings: Settings) -> None:
    if secret != settings.COLAB_SECRET:
        raise HTTPException(status_code=403, detail="Неверный COLAB_SECRET")

def _ext(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

# ── POST /upload ───────────────────────────────────────────────
@router.post("/upload", status_code=status.HTTP_201_CREATED)
async def upload_track(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    storage: LocalStorageAdapter = Depends(get_storage),
    settings: Settings = Depends(get_settings),
):
    ext = _ext(file.filename or "")
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Формат .{ext} не поддерживается")

    project = Project(title=Path(file.filename or "track").stem)
    db.add(project)
    await db.flush()

    original_filename = f"original.{ext}"
    try:
        original_path = await storage.put(project.id, file, original_filename)
    except ValueError as exc:
        raise HTTPException(status_code=413, detail=str(exc))

    try:
        wav_path = await convert_to_wav(original_path)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=f"Конвертация: {exc}")

    if wav_path != original_path:
        original_path.unlink(missing_ok=True)

    canonical = storage.get_path(project.id, "original.wav")
    if wav_path != canonical:
        wav_path.rename(canonical)
        wav_path = canonical

    duration_ms = await get_duration(wav_path)

    asset = AudioAsset(
        project_id=project.id, type="original",
        file_path=str(wav_path), duration_ms=duration_ms,
        format="wav", size_bytes=wav_path.stat().st_size,
    )
    db.add(asset)

    # BUG-1 FIX: timestamp-based ключ, не :v1
    sep_key = f"{project.id}:separation:{int(_time.time())}"
    job = Job(
        project_id=project.id, type="separation", status="pending",
        idempotency_key=sep_key,
        input_params=json.dumps({"file_path": str(wav_path), "duration_ms": duration_ms}),
    )
    db.add(job)
    await db.flush()

    logger.info("upload: project=%s job=%s", project.id, job.id)
    return {"project_id": project.id, "job_id": job.id, "status": project.status}


# ── GET /jobs/pending ──────────────────────────────────────────
@router.get("/jobs/pending")
async def get_pending_jobs(
    secret: str = Query(...),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    _check_secret(secret, settings)
    result = await db.execute(
        select(Job).where(
            Job.status == "pending",
            Job.type.in_(["separation", "transcription"]),
        ).order_by(Job.created_at)
    )
    jobs = result.scalars().all()
    return [
        {"job_id": j.id, "project_id": j.project_id, "type": j.type,
         "input_params": json.loads(j.input_params) if j.input_params else {}}
        for j in jobs
    ]


# ── POST /jobs/{id}/claim ──────────────────────────────────────
@router.post("/jobs/{job_id}/claim")
async def claim_job(
    job_id: str,
    secret: str = Query(...),
    db: AsyncSession = Depends(get_db),
    storage: LocalStorageAdapter = Depends(get_storage),
    settings: Settings = Depends(get_settings),
):
    _check_secret(secret, settings)
    res = await db.execute(select(Job).where(Job.id == job_id))
    job: Job | None = res.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Job не найден")
    if job.status != "pending":
        raise HTTPException(status_code=409, detail=f"Job статус: {job.status}")

    job.status = "processing"

    proj_res = await db.execute(select(Project).where(Project.id == job.project_id))
    project: Project | None = proj_res.scalar_one_or_none()
    if project:
        project.status = "processing"
        project.pipeline_state = "separating" if job.type == "separation" else "transcribing"

    asset_type = "original" if job.type == "separation" else "vocal"
    download_url = f"{settings.base_url}/api/projects/{job.project_id}/assets/{asset_type}"

    logger.info("claim: job=%s type=%s url=%s", job_id, job.type, download_url)
    return {"job_id": job.id, "status": "processing", "download_url": download_url}


# ── POST /jobs/{id}/complete ───────────────────────────────────
@router.post("/jobs/{job_id}/complete")
async def complete_job(
    job_id: str,
    vocal_file: UploadFile = File(...),
    instrumental_file: UploadFile = File(...),
    secret: str = Query(...),
    db: AsyncSession = Depends(get_db),
    storage: LocalStorageAdapter = Depends(get_storage),
    settings: Settings = Depends(get_settings),
):
    _check_secret(secret, settings)
    res = await db.execute(select(Job).where(Job.id == job_id))
    job: Job | None = res.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Job не найден")
    if job.status != "processing":
        raise HTTPException(status_code=409, detail=f"Job статус: {job.status}")

    project_id = job.project_id

    # Сохраняем файлы
    vocal_ext = _ext(vocal_file.filename or "wav")
    vocal_path = await storage.put(project_id, vocal_file, f"vocal.{vocal_ext}")
    inst_ext   = _ext(instrumental_file.filename or "wav")
    inst_path  = await storage.put(project_id, instrumental_file, f"instrumental.{inst_ext}")

    # BUG-7 FIX: конвертируем в WAV если пришёл MP3/FLAC
    if vocal_ext != "wav":
        try:
            wav = await convert_to_wav(vocal_path)
            vocal_path.unlink(missing_ok=True)
            vocal_path = wav
            vocal_ext  = "wav"
        except Exception as e:
            logger.warning("vocal conversion failed: %s", e)

    if inst_ext != "wav":
        try:
            wav = await convert_to_wav(inst_path)
            inst_path.unlink(missing_ok=True)
            inst_path = wav
            inst_ext  = "wav"
        except Exception as e:
            logger.warning("instrumental conversion failed: %s", e)

    vocal_dur = await get_duration(vocal_path)
    inst_dur  = await get_duration(inst_path)

    db.add(AudioAsset(
        project_id=project_id, type="vocal",
        file_path=str(vocal_path), duration_ms=vocal_dur,
        format=vocal_ext, size_bytes=vocal_path.stat().st_size,
    ))
    db.add(AudioAsset(
        project_id=project_id, type="instrumental",
        file_path=str(inst_path), duration_ms=inst_dur,
        format=inst_ext, size_bytes=inst_path.stat().st_size,
    ))

    job.status = "done"
    job.output_result = json.dumps({"vocal": str(vocal_path), "instrumental": str(inst_path)})

    proj_res = await db.execute(select(Project).where(Project.id == project_id))
    project: Project | None = proj_res.scalar_one_or_none()
    if project:
        project.status         = "processing"
        project.pipeline_state = "transcribing"

    # BUG-1 FIX: timestamp-based ключ
    trans_key = f"{project_id}:transcription:{int(_time.time())}"
    trans_job = Job(
        project_id=project_id, type="transcription", status="pending",
        idempotency_key=trans_key,
        input_params=json.dumps({"vocal_path": str(vocal_path)}),
    )
    db.add(trans_job)
    await db.flush()

    logger.info("complete: job=%s → transcription job=%s", job_id, trans_job.id)
    return {"status": "ok", "next_job": "transcription"}


# ── GET /projects/{id}/status ─────────────────────────────
@router.get("/projects/{project_id}/status")
async def get_project_status(project_id: str, db: AsyncSession = Depends(get_db)):
    """Лёгкий endpoint для live поллинга статуса проекта."""
    res = await db.execute(select(Project).where(Project.id == project_id))
    project: Project | None = res.scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="Проект не найден")
    return {
        "status": project.status,
        "pipeline_state": project.pipeline_state,
    }


# ── GET /projects/{id}/assets/{type} ──────────────────────────
@router.get("/projects/{project_id}/assets/{asset_type}")
async def get_asset(project_id: str, asset_type: str, db: AsyncSession = Depends(get_db)):
    if asset_type not in {"original", "vocal", "instrumental"}:
        raise HTTPException(status_code=400, detail="Неверный тип")
    res = await db.execute(
        select(AudioAsset).where(
            AudioAsset.project_id == project_id, AudioAsset.type == asset_type
        )
    )
    asset: AudioAsset | None = res.scalar_one_or_none()
    if asset is None:
        raise HTTPException(status_code=404, detail="Ассет не найден")
    fpath = Path(asset.file_path)
    if not fpath.exists():
        raise HTTPException(status_code=404, detail="Файл не найден")
    media_map = {"wav":"audio/wav","mp3":"audio/mpeg","flac":"audio/flac","m4a":"audio/mp4"}
    return FileResponse(str(fpath), media_type=media_map.get(asset.format or "wav","application/octet-stream"), filename=fpath.name)


# ── DELETE /projects/{id} ─────────────────────────────────────
@router.delete("/projects/{project_id}")
async def delete_project(
    project_id: str,
    db: AsyncSession = Depends(get_db),
    storage: LocalStorageAdapter = Depends(get_storage),
):
    res = await db.execute(select(Project).where(Project.id == project_id))
    project: Project | None = res.scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="Проект не найден")
    storage.delete(project_id)
    await db.delete(project)
    logger.info("delete_project: %s", project_id)
    return {"status": "deleted"}
