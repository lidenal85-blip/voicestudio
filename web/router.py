"""
web/router.py
Jinja2 HTML-роутеры.
Фикс: TemplateResponse использует старый API (совместим со всеми версиями Starlette):
  templates.TemplateResponse("name.html", {"request": request, ...})
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from config.settings import get_settings
from db.init_db import get_db
from db.models import AudioAsset, Project, Transcription

logger = logging.getLogger(__name__)
settings = get_settings()

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(tags=["web"], include_in_schema=False)


# ── Helpers ────────────────────────────────────────────────────────────────────

async def _get_project_or_404(db: AsyncSession, project_id: str) -> Project:
    res = await db.execute(select(Project).where(Project.id == project_id))
    p = res.scalar_one_or_none()
    if p is None:
        raise HTTPException(status_code=404, detail="Проект не найден")
    return p


async def _get_assets_map(db: AsyncSession, project_id: str) -> Dict[str, Any]:
    res = await db.execute(
        select(AudioAsset).where(AudioAsset.project_id == project_id)
    )
    return {a.type: a for a in res.scalars().all()}


async def _get_active_transcription(db: AsyncSession, project_id: str):
    res = await db.execute(
        select(Transcription)
        .where(Transcription.project_id == project_id, Transcription.is_active == True)  # noqa
        .options(selectinload(Transcription.lines))
    )
    return res.scalar_one_or_none()


def _r(request, name, context):
    """Совместимый вызов TemplateResponse для любой версии Starlette."""
    import os
    base = os.environ.get("ROOT_PATH", "").rstrip("/")
    ctx = {"request": request, "base": base, **context}
    return templates.TemplateResponse(name, ctx)


# ── GET / ──────────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def page_index(request: Request, db: AsyncSession = Depends(get_db)):
    try:
        res = await db.execute(select(Project).order_by(Project.created_at.desc()))
        projects = res.scalars().all()
    except Exception as exc:
        logger.error("page_index DB: %s", exc)
        projects = []
    return _r(request, "index.html", {"projects": projects})


# ── GET /upload ────────────────────────────────────────────────────────────────

@router.get("/upload", response_class=HTMLResponse)
async def page_upload(request: Request):
    return _r(request, "upload.html", {"max_size_mb": settings.MAX_FILE_SIZE_MB})


# ── GET /projects/{id} ────────────────────────────────────────────────────────

@router.get("/projects/{project_id}", response_class=HTMLResponse)
async def page_project(request: Request, project_id: str, db: AsyncSession = Depends(get_db)):
    project    = await _get_project_or_404(db, project_id)
    assets_map = await _get_assets_map(db, project_id)
    tr         = await _get_active_transcription(db, project_id)
    types = [
        ("original",     "Оригинал",     assets_map.get("original")),
        ("vocal",        "Вокал",        assets_map.get("vocal")),
        ("instrumental", "Инструментал", assets_map.get("instrumental")),
    ]
    return _r(request, "project.html", {
        "project":           project,
        "assets_map":        assets_map,
        "types":             types,
        "has_transcription": tr is not None,
    })


# ── GET /projects/{id}/player ──────────────────────────────────────────────────

@router.get("/projects/{project_id}/player", response_class=HTMLResponse)
async def page_player(request: Request, project_id: str, db: AsyncSession = Depends(get_db)):
    project    = await _get_project_or_404(db, project_id)
    assets_map = await _get_assets_map(db, project_id)
    tr         = await _get_active_transcription(db, project_id)
    return _r(request, "player.html", {
        "project":           project,
        "assets_map":        assets_map,
        "has_transcription": tr is not None,
        "lrc_lines":         tr.lines if tr else [],
        "lrc_json":          json.dumps(tr.lrc_content or "") if tr else '""',
    })


# ── GET /projects/{id}/editor ─────────────────────────────────────────────────

@router.get("/projects/{project_id}/editor", response_class=HTMLResponse)
async def page_editor(request: Request, project_id: str, db: AsyncSession = Depends(get_db)):
    project = await _get_project_or_404(db, project_id)
    tr      = await _get_active_transcription(db, project_id)
    if tr is None:
        raise HTTPException(status_code=404, detail="Транскрипция ещё не готова")
    assets_map = await _get_assets_map(db, project_id)
    lines_data = [
        {"index": ln.index, "text": ln.text,
         "start_ms": ln.start_ms, "end_ms": ln.end_ms, "version": ln.version}
        for ln in tr.lines
    ]
    return _r(request, "editor.html", {
        "project":       project,
        "transcription": tr,
        "lrc_lines":     tr.lines,
        "assets_map":    assets_map,
        "lines_json":    json.dumps(lines_data, ensure_ascii=False),
    })


# ── GET /projects/{id}/record ─────────────────────────────────────────────────

@router.get("/projects/{project_id}/record", response_class=HTMLResponse)
async def page_record(request: Request, project_id: str, db: AsyncSession = Depends(get_db)):
    project    = await _get_project_or_404(db, project_id)
    assets_map = await _get_assets_map(db, project_id)
    return _r(request, "record.html", {
        "project":          project,
        "assets_map":       assets_map,
        "has_instrumental": "instrumental" in assets_map,
    })


# ── GET /projects/{id}/history ────────────────────────────────────────────────

@router.get("/projects/{project_id}/history", response_class=HTMLResponse)
async def page_history(request: Request, project_id: str, db: AsyncSession = Depends(get_db)):
    project    = await _get_project_or_404(db, project_id)
    assets_map = await _get_assets_map(db, project_id)
    trs_res    = await db.execute(
        select(Transcription)
        .where(Transcription.project_id == project_id)
        .options(selectinload(Transcription.lines))
        .order_by(Transcription.version.desc())
    )
    transcriptions = trs_res.scalars().all()
    trs_data = [
        type("TR", (), {
            "id":          tr.id,
            "version":     tr.version,
            "language":    tr.language,
            "confidence":  tr.confidence,
            "lines_count": len(tr.lines),
            "is_active":   tr.is_active,
            "created_at":  tr.created_at,
        })()
        for tr in transcriptions
    ]
    return _r(request, "history.html", {
        "project":           project,
        "transcriptions":    trs_data,
        "has_transcription": any(tr.is_active for tr in transcriptions),
        "has_vocal":         "vocal" in assets_map,
        "assets_map":        assets_map,
    })


# ── Redirect ───────────────────────────────────────────────────────────────────

@router.get("/projects/", response_class=RedirectResponse, include_in_schema=False)
async def projects_redirect():
    return RedirectResponse(url="/")
