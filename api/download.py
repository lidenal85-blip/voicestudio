"""
api/download.py
Скачивание треков через yt-dlp прямо в Voice Studio.
POST /api/download — принимает URL, скачивает аудио, создаёт проект.
GET  /api/download/info — получить метаданные без скачивания.
"""
from __future__ import annotations
import asyncio
import json
import logging
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import Settings, get_settings
from db.init_db import get_db
from db.models import AudioAsset, Job, Project
from services.converter import convert_to_wav, get_duration
from services.storage import LocalStorageAdapter, get_storage

logger = logging.getLogger(__name__)
router = APIRouter()

SUPPORTED_SITES = [
    "youtube.com", "youtu.be",
    "soundcloud.com",
    "spotify.com",
    "vk.com", "vkvideo.ru",
    "rutube.ru",
    "bandcamp.com",
    "music.yandex.ru",
]


class DownloadRequest(BaseModel):
    url:   str
    title: str = ""


class InfoRequest(BaseModel):
    url: str


def _is_yt_dlp_available() -> bool:
    try:
        import yt_dlp  # noqa: F401
        return True
    except ImportError:
        return False


async def _download_audio(url: str, out_dir: Path) -> tuple[Path, str]:
    """Скачивает аудио через yt-dlp в out_dir. Возвращает (path_to_file, title)."""
    import yt_dlp

    ydl_opts = {
        "format":            "bestaudio/best",
        "outtmpl":           str(out_dir / "%(title).80s.%(ext)s"),
        "postprocessors": [{
            "key":            "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
        "noplaylist":        True,
        "quiet":             True,
        "no_warnings":       True,
        "max_filesize":      200 * 1024 * 1024,
    }

    title = ""

    def _run():
        nonlocal title
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info:
                title = info.get("title", "") or info.get("id", "track")
        files = list(out_dir.glob("*.mp3")) + list(out_dir.glob("*.m4a")) + list(out_dir.glob("*.webm"))
        if not files:
            raise FileNotFoundError("yt-dlp: no output file found")
        return sorted(files, key=lambda f: f.stat().st_mtime)[-1], title

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run)


async def _search_tracks(query: str, limit: int = 8) -> list[dict]:
    """Поиск треков по названию через yt-dlp (YouTube search)."""
    import yt_dlp

    def _run():
        opts = {
            "quiet":        True,
            "no_warnings":  True,
            "extract_flat": True,
            "noplaylist":   False,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            data = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
        entries = data.get("entries") or []
        results = []
        for e in entries:
            if not e:
                continue
            url = e.get("url") or e.get("webpage_url") or ""
            if not url.startswith("http"):
                url = f"https://www.youtube.com/watch?v={e.get('id', '')}"
            results.append({
                "title":      e.get("title", ""),
                "uploader":   e.get("uploader") or e.get("channel", ""),
                "duration":   e.get("duration") or 0,
                "thumbnail":  e.get("thumbnail", ""),
                "url":        url,
                "view_count": e.get("view_count") or 0,
            })
        return results

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run)


async def _get_info(url: str) -> dict:
    """Получает метаданные без скачивания."""
    import yt_dlp

    def _run():
        opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return {
                "title":     info.get("title", ""),
                "uploader":  info.get("uploader", ""),
                "duration":  info.get("duration", 0),
                "thumbnail": info.get("thumbnail", ""),
            }

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run)


# ── GET /api/download/search ──────────────────────────────────
@router.get("/download/search")
async def search_tracks(q: str, limit: int = 8):
    """Поиск треков по названию. Возвращает список результатов с YouTube."""
    if not _is_yt_dlp_available():
        raise HTTPException(status_code=503, detail="yt-dlp не установлен")
    q = q.strip()
    if not q:
        raise HTTPException(status_code=400, detail="Пустой запрос")
    if len(q) > 200:
        raise HTTPException(status_code=400, detail="Запрос слишком длинный")
    limit = max(1, min(limit, 12))
    try:
        results = await asyncio.wait_for(_search_tracks(q, limit), timeout=20)
        return {"results": results, "query": q}
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Поиск занял слишком долго")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Ошибка поиска: {exc}")


# ── GET /api/download/info ────────────────────────────────────
@router.get("/download/info")
async def get_download_info(url: str):
    if not _is_yt_dlp_available():
        raise HTTPException(status_code=503, detail="yt-dlp не установлен на сервере")
    try:
        info = await asyncio.wait_for(_get_info(url), timeout=30)
        return info
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Таймаут при получении информации о треке")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Не удалось получить информацию: {exc}")


# ── POST /api/download ────────────────────────────────────────
@router.post("/download", status_code=201)
async def download_track(
    body:     DownloadRequest,
    db:       AsyncSession = Depends(get_db),
    storage:  LocalStorageAdapter = Depends(get_storage),
    settings: Settings = Depends(get_settings),
):
    if not _is_yt_dlp_available():
        raise HTTPException(status_code=503, detail="yt-dlp не установлен на сервере")

    if not body.url.strip():
        raise HTTPException(status_code=400, detail="URL не может быть пустым")

    project = Project(title=body.title or "downloading…")
    db.add(project)
    await db.flush()
    project_id = project.id

    tmpdir = Path(tempfile.mkdtemp(prefix="vs_download_"))
    try:
        logger.info("download: project=%s url=%s", project_id[:8], body.url[:60])

        try:
            audio_path, detected_title = await _download_audio(body.url, tmpdir)
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail="Таймаут скачивания (>5 мин)")
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Ошибка скачивания: {exc}")

        final_title = body.title.strip() or detected_title or audio_path.stem
        project.title = final_title[:255]

        try:
            wav_path = await convert_to_wav(audio_path)
            audio_path.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning("download: wav conversion failed (%s), using original", exc)
            wav_path = audio_path

        ext = wav_path.suffix.lstrip(".")
        dest_path = storage._project_dir(project_id) / f"original.{ext}"
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(wav_path), str(dest_path))

        duration_ms = await get_duration(dest_path)
        size_bytes  = dest_path.stat().st_size

        if size_bytes > settings.MAX_FILE_SIZE_BYTES:
            dest_path.unlink(missing_ok=True)
            raise HTTPException(
                status_code=413,
                detail=f"Файл {size_bytes // 1024 // 1024}MB превышает лимит {settings.MAX_FILE_SIZE_MB}MB",
            )

        db.add(AudioAsset(
            project_id=project_id, type="original",
            file_path=str(dest_path), duration_ms=duration_ms,
            format=ext, size_bytes=size_bytes,
        ))
        project.status         = "created"
        project.pipeline_state = "uploaded"

        import time as _time
        job = Job(
            project_id=project_id,
            type="separation",
            status="pending",
            idempotency_key=f"{project_id}:separation:{int(_time.time())}",
            input_params=json.dumps({"file_path": str(dest_path), "duration_ms": duration_ms}),
        )
        db.add(job)
        await db.commit()

        logger.info(
            "download: OK project=%s title='%s' duration=%ds",
            project_id[:8], final_title, (duration_ms or 0) // 1000,
        )
        return {"project_id": project_id, "title": final_title, "duration_ms": duration_ms}

    except HTTPException:
        raise
    except Exception as exc:
        logger.error("download: unexpected error: %s", exc)
        raise HTTPException(status_code=500, detail=f"Внутренняя ошибка: {exc}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
