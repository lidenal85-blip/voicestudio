"""
services/slicer.py
FFmpeg-нарезка аудио по LyricLine-тайм-кодам.
Запускается как фоновая asyncio-задача прямо на телефоне (Termux).

Особенности:
- Delta update: принимает список индексов для нарезки (не всё)
- Fade in/out 30мс — убирает щелчки
- Оптимистичная блокировка: пропускает строку если version изменился (аудитор п.2.1)
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_FADE_MS      = 30     # fade in/out для каждой фразы
_LUFS_TARGET  = -16.0  # целевая громкость (LUFS)


async def slice_phrase(
    source_path: Path,
    dest_path: Path,
    start_ms: int,
    end_ms: int,
    fade_ms: int = _FADE_MS,
) -> Path:
    """
    Вырезает один фрагмент из аудиофайла.
    source_path  — исходный WAV (вокал)
    dest_path    — куда сохранить phrase_{index}.wav
    start_ms     — начало фрагмента
    end_ms       — конец фрагмента
    """
    duration_ms = max(end_ms - start_ms, 100)   # минимум 100мс
    start_sec   = start_ms / 1000
    dur_sec     = duration_ms / 1000
    fade_sec    = fade_ms / 1000

    af_filter = (
        f"afade=t=in:st=0:d={fade_sec},"
        f"afade=t=out:st={max(dur_sec - fade_sec, 0):.3f}:d={fade_sec}"
    )

    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{start_sec:.3f}",
        "-t",  f"{dur_sec:.3f}",
        "-i",  str(source_path),
        "-af", af_filter,
        "-acodec", "pcm_s16le",
        "-ar",     "44100",
        "-ac",     "2",
        str(dest_path),
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()

    if proc.returncode != 0:
        raise RuntimeError(
            f"FFmpeg phrase slice failed: {stderr.decode(errors='replace')[-300:]}"
        )

    return dest_path


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


async def run_slicing(
    project_id: str,
    transcription_id: str,
    vocal_path: Path,
    output_dir: Path,
    line_indices: list[int] | None = None,   # None = все строки
) -> dict[int, Path]:
    """
    Нарезает фразы из вокала по LyricLine из БД.

    line_indices — если передан, нарезает только эти индексы (Delta Update).
    Возвращает { index: phrase_path }.

    Реализует оптимистичную блокировку: если version строки изменился
    с момента начала задачи — строка пропускается.
    """
    from db.init_db import _get_session_factory
    from db.models import AudioAsset, LyricLine, Transcription
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    output_dir.mkdir(parents=True, exist_ok=True)
    result: dict[int, Path] = {}

    async with _get_session_factory()() as session:
        # Загружаем строки
        stmt = (
            select(Transcription)
            .where(Transcription.id == transcription_id)
            .options(selectinload(Transcription.lines))
        )
        tr = (await session.execute(stmt)).scalar_one_or_none()
        if tr is None:
            logger.error("run_slicing: transcription %s not found", transcription_id)
            return {}

        lines = tr.lines
        if line_indices is not None:
            lines = [ln for ln in lines if ln.index in line_indices]

        logger.info(
            "run_slicing: project=%s lines=%d source=%s",
            project_id, len(lines), vocal_path.name,
        )

        for ln in lines:
            # Снимаем версию ДО нарезки
            version_before = ln.version

            dest = output_dir / f"phrase_{ln.index:04d}.wav"
            try:
                await slice_phrase(
                    source_path = vocal_path,
                    dest_path   = dest,
                    start_ms    = ln.start_ms,
                    end_ms      = ln.end_ms,
                )
            except Exception as exc:
                logger.warning("slice_phrase[%d] failed: %s", ln.index, exc)
                continue

            # Optimistic lock: проверяем что версия не изменилась пока нарезали
            await session.refresh(ln)
            if ln.version != version_before:
                logger.info(
                    "slice_phrase[%d]: version changed %d→%d, skipping DB update",
                    ln.index, version_before, ln.version,
                )
                dest.unlink(missing_ok=True)
                continue

            # Создаём или обновляем AudioAsset
            asset = AudioAsset(
                project_id  = project_id,
                type        = "phrase",
                file_path   = str(dest),
                format      = "wav",
                size_bytes  = dest.stat().st_size,
                checksum    = _sha256(dest),
            )
            session.add(asset)
            await session.flush()

            ln.phrase_asset_id = asset.id
            result[ln.index] = dest

        await session.commit()

    logger.info("run_slicing: done, sliced=%d", len(result))
    return result
