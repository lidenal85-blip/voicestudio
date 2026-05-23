"""
api/transcription.py — Транскрипция и нарезка фраз.
Fixes:
  - BUG-3: исправлен output_dir (убран двойной project_id)
  - BUG-5: SlicingJob создаётся как pending, не processing
  - BUG-1: idempotency_key timestamp-based
"""
from __future__ import annotations
import json, logging, time as _time
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from config.settings import Settings, get_settings
from db.init_db import get_db
from db.models import AudioAsset, Job, LyricLine, Project, Transcription
from infrastructure.ws import ws_manager
from services.lrc import LrcLine, compute_confidence, segments_to_lrc, segments_to_lrc_lines
from services.slicer import run_slicing
from services.storage import LocalStorageAdapter, get_storage

logger = logging.getLogger(__name__)
router = APIRouter()


class TranscriptionCompleteBody(BaseModel):
    segments_json: str
    language:      str = "ru"
    model_used:    str = "base"


class LineEdit(BaseModel):
    index:    int
    text:     str | None = None
    start_ms: int | None = None
    end_ms:   int | None = None


class DeltaEditBody(BaseModel):
    changed_lines: list[LineEdit]


def _check_secret(secret: str, settings: Settings) -> None:
    if secret != settings.COLAB_SECRET:
        raise HTTPException(status_code=403, detail="Неверный COLAB_SECRET")


# ── POST /jobs/{id}/complete-transcription ────────────────────
@router.post("/jobs/{job_id}/complete-transcription")
async def complete_transcription(
    job_id: str,
    body: TranscriptionCompleteBody,
    background_tasks: BackgroundTasks,
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
    if job.type != "transcription":
        raise HTTPException(status_code=400, detail="Job не транскрипция")
    if job.status != "processing":
        raise HTTPException(status_code=409, detail=f"Job статус: {job.status}")

    project_id = job.project_id

    try:
        segments: list[dict] = json.loads(body.segments_json)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Невалидный segments_json: {e}")

    lrc_lines:   list[LrcLine] = segments_to_lrc_lines(segments)
    lrc_content: str           = segments_to_lrc(segments)
    confidence:  float         = compute_confidence(segments)

    # Деактивируем предыдущие транскрипции
    prev = await db.execute(
        select(Transcription).where(
            Transcription.project_id == project_id,
            Transcription.is_active  == True,  # noqa
        )
    )
    prev_version = 0
    for old_tr in prev.scalars().all():
        old_tr.is_active = False
        prev_version = max(prev_version, old_tr.version)

    transcription = Transcription(
        project_id=project_id, version=prev_version + 1,
        language=body.language, confidence=confidence,
        lrc_content=lrc_content, segments_json=body.segments_json, is_active=True,
    )
    db.add(transcription)
    await db.flush()

    for ll in lrc_lines:
        db.add(LyricLine(
            transcription_id=transcription.id, index=ll.index,
            text=ll.text, start_ms=ll.start_ms, end_ms=ll.end_ms,
        ))

    job.status        = "done"
    job.output_result = json.dumps({
        "transcription_id": transcription.id, "lines": len(lrc_lines),
        "language": body.language, "confidence": confidence,
    })

    # BUG-1 FIX: timestamp key
    # BUG-5 FIX: status=pending (не processing), фоновая задача сама меняет статус
    slicing_key = f"{project_id}:slicing:{int(_time.time())}"
    slicing_job = Job(
        project_id=project_id, type="slicing",
        status="pending",   # ← FIX: было processing
        idempotency_key=slicing_key,
        input_params=json.dumps({
            "transcription_id": transcription.id,
            "line_count": len(lrc_lines),
        }),
    )
    db.add(slicing_job)

    proj_res = await db.execute(select(Project).where(Project.id == project_id))
    project: Project | None = proj_res.scalar_one_or_none()
    if project:
        project.pipeline_state = "slicing"

    await db.flush()
    slicing_job_id   = slicing_job.id
    transcription_id = transcription.id
    await db.commit()

    # Находим вокал
    asset_res = await db.execute(
        select(AudioAsset).where(AudioAsset.project_id == project_id, AudioAsset.type == "vocal")
    )
    vocal_asset: AudioAsset | None = asset_res.scalar_one_or_none()
    vocal_path = Path(vocal_asset.file_path) if vocal_asset else None

    # BUG-3 FIX: правильный output_dir без двойного project_id
    output_dir = storage._project_dir(project_id) / "phrases"

    if vocal_path and vocal_path.exists():
        background_tasks.add_task(
            _run_slicing_background,
            project_id=project_id, transcription_id=transcription_id,
            slicing_job_id=slicing_job_id, vocal_path=vocal_path, output_dir=output_dir,
        )
    else:
        logger.warning("vocal not found for %s, skipping slicing", project_id)
        background_tasks.add_task(_mark_ready, project_id, slicing_job_id)

    logger.info("complete_transcription: project=%s lines=%d", project_id, len(lrc_lines))
    return {"status": "ok", "transcription_id": transcription_id,
            "lines": len(lrc_lines), "slicing_job_id": slicing_job_id}


async def _run_slicing_background(
    project_id: str, transcription_id: str,
    slicing_job_id: str, vocal_path: Path, output_dir: Path,
) -> None:
    from db.init_db import _get_session_factory
    # BUG-5 FIX: переводим в processing перед началом работы
    async with _get_session_factory()() as s:
        res = await s.execute(select(Job).where(Job.id == slicing_job_id))
        j   = res.scalar_one_or_none()
        if j: j.status = "processing"
        await s.commit()

    await ws_manager.send_progress(project_id, "slicing", 0, "Нарезка фраз")
    try:
        result = await run_slicing(
            project_id=project_id, transcription_id=transcription_id,
            vocal_path=vocal_path, output_dir=output_dir,
        )
        await ws_manager.send_progress(project_id, "slicing", 100, f"{len(result)} фраз")
    except Exception as exc:
        logger.error("slicing error: %s", exc)
        await ws_manager.send_error(project_id, str(exc))
        await _mark_failed(project_id, slicing_job_id, str(exc))
        return

    await _mark_ready(project_id, slicing_job_id)
    await ws_manager.send_done(project_id)


async def _mark_ready(project_id: str, job_id: str) -> None:
    from db.init_db import _get_session_factory
    async with _get_session_factory()() as s:
        res = await s.execute(select(Project).where(Project.id == project_id))
        p   = res.scalar_one_or_none()
        if p: p.status = "ready"; p.pipeline_state = "ready"
        res2 = await s.execute(select(Job).where(Job.id == job_id))
        j    = res2.scalar_one_or_none()
        if j: j.status = "done"
        await s.commit()


async def _mark_failed(project_id: str, job_id: str, error: str) -> None:
    from db.init_db import _get_session_factory
    async with _get_session_factory()() as s:
        res = await s.execute(select(Job).where(Job.id == job_id))
        j   = res.scalar_one_or_none()
        if j: j.status = "failed"; j.error_msg = error
        await s.commit()


# ── GET /projects/{id}/transcription ──────────────────────────
@router.get("/projects/{project_id}/transcription")
async def get_transcription(project_id: str, db: AsyncSession = Depends(get_db)):
    res = await db.execute(
        select(Transcription).where(
            Transcription.project_id == project_id,
            Transcription.is_active  == True,  # noqa
        ).options(selectinload(Transcription.lines))
    )
    tr: Transcription | None = res.scalar_one_or_none()
    if tr is None:
        raise HTTPException(status_code=404, detail="Транскрипция не найдена")
    return {
        "transcription_id": tr.id, "version": tr.version,
        "language": tr.language, "confidence": tr.confidence,
        "lrc_content": tr.lrc_content, "created_at": tr.created_at.isoformat(),
        "lines": [
            {"id": ln.id, "index": ln.index, "text": ln.text,
             "start_ms": ln.start_ms, "end_ms": ln.end_ms,
             "is_edited": ln.is_edited, "version": ln.version,
             "phrase_asset_id": ln.phrase_asset_id}
            for ln in tr.lines
        ],
    }


# ── PATCH /projects/{id}/transcription/lines ──────────────────
@router.patch("/projects/{project_id}/transcription/lines")
async def patch_transcription_lines(
    project_id: str, body: DeltaEditBody,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    storage: LocalStorageAdapter = Depends(get_storage),
):
    if not body.changed_lines:
        raise HTTPException(status_code=400, detail="changed_lines пустой")

    res = await db.execute(
        select(Transcription).where(
            Transcription.project_id == project_id,
            Transcription.is_active  == True,  # noqa
        ).options(selectinload(Transcription.lines))
    )
    tr: Transcription | None = res.scalar_one_or_none()
    if tr is None:
        raise HTTPException(status_code=404, detail="Транскрипция не найдена")

    lines_by_index    = {ln.index: ln for ln in tr.lines}
    updated_indices: list[int] = []

    for edit in body.changed_lines:
        ln = lines_by_index.get(edit.index)
        if not ln: continue
        if edit.text     is not None: ln.text     = edit.text
        if edit.start_ms is not None: ln.start_ms = edit.start_ms
        if edit.end_ms   is not None: ln.end_ms   = edit.end_ms
        ln.is_edited = True
        ln.version  += 1
        updated_indices.append(ln.index)

    if not updated_indices:
        raise HTTPException(status_code=404, detail="Строки не найдены")

    # Отменяем предыдущие SlicingJob
    prev_jobs = await db.execute(
        select(Job).where(
            Job.project_id == project_id, Job.type == "slicing",
            Job.status.in_(["pending", "processing"]),
        )
    )
    for old_job in prev_jobs.scalars().all():
        old_job.status = "cancelled"
        logger.info("delta_edit: cancelled slicing %s", old_job.id[:8])

    # BUG-1 FIX + BUG-5 FIX
    new_key = f"{project_id}:slicing:{int(_time.time())}"
    new_job = Job(
        project_id=project_id, type="slicing",
        status="pending",  # ← FIX
        idempotency_key=new_key,
        input_params=json.dumps({
            "transcription_id": tr.id, "changed_indices": updated_indices,
        }),
    )
    db.add(new_job)
    await db.flush()
    new_job_id = new_job.id
    await db.commit()

    asset_res = await db.execute(
        select(AudioAsset).where(AudioAsset.project_id == project_id, AudioAsset.type == "vocal")
    )
    vocal_asset = asset_res.scalar_one_or_none()
    if vocal_asset:
        vocal_path = Path(vocal_asset.file_path)
        # BUG-3 FIX
        output_dir = storage._project_dir(project_id) / "phrases"
        background_tasks.add_task(
            _run_slicing_background,
            project_id=project_id, transcription_id=tr.id,
            slicing_job_id=new_job_id, vocal_path=vocal_path, output_dir=output_dir,
        )

    return {"updated_lines": updated_indices, "slicing_job_id": new_job_id}


# ── GET /projects/{id}/phrases/{index} ────────────────────────
@router.get("/projects/{project_id}/phrases/{index}")
async def get_phrase(project_id: str, index: int, db: AsyncSession = Depends(get_db)):
    res = await db.execute(
        select(Transcription).where(
            Transcription.project_id == project_id, Transcription.is_active == True  # noqa
        ).options(selectinload(Transcription.lines))
    )
    tr = res.scalar_one_or_none()
    if tr is None:
        raise HTTPException(status_code=404, detail="Транскрипция не найдена")
    ln = {l.index: l for l in tr.lines}.get(index)
    if ln is None or ln.phrase_asset_id is None:
        raise HTTPException(status_code=404, detail="Фраза ещё не нарезана")
    asset_res = await db.execute(select(AudioAsset).where(AudioAsset.id == ln.phrase_asset_id))
    asset: AudioAsset | None = asset_res.scalar_one_or_none()
    if asset is None:
        raise HTTPException(status_code=404, detail="Ассет не найден")
    fpath = Path(asset.file_path)
    if not fpath.exists():
        raise HTTPException(status_code=404, detail="Файл не найден")
    return FileResponse(str(fpath), media_type="audio/wav", filename=fpath.name)
