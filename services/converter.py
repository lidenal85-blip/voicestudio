"""
Модуль 5: services/converter.py
FFmpeg-обёртка: конвертация в WAV + получение длительности.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

# Целевые параметры WAV
_WAV_SAMPLE_RATE = "44100"
_WAV_BIT_DEPTH   = "pcm_s16le"  # 16-bit
_WAV_CHANNELS    = "2"          # стерео


def _ffmpeg_bin() -> str:
    """Проверяет наличие ffmpeg в PATH."""
    path = shutil.which("ffmpeg")
    if path is None:
        raise RuntimeError(
            "ffmpeg не найден. Установи: pkg install ffmpeg (Termux)"
        )
    return path


def _ffprobe_bin() -> str:
    path = shutil.which("ffprobe")
    if path is None:
        raise RuntimeError(
            "ffprobe не найден. Установи: pkg install ffmpeg (Termux)"
        )
    return path


async def convert_to_wav(input_path: Path) -> Path:
    """
    Конвертирует аудиофайл в WAV 16-bit / 44.1 kHz / stereo.
    Возвращает путь к WAV-файлу (рядом с оригиналом).
    """
    output_path = input_path.with_suffix(".wav")
    if output_path == input_path:
        # Уже WAV — просто нормализуем
        output_path = input_path.with_name(input_path.stem + "_norm.wav")

    cmd = [
        _ffmpeg_bin(),
        "-y",                        # перезаписать без вопросов
        "-i", str(input_path),
        "-acodec", _WAV_BIT_DEPTH,
        "-ar", _WAV_SAMPLE_RATE,
        "-ac", _WAV_CHANNELS,
        str(output_path),
    ]

    logger.debug("convert_to_wav: %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()

    if proc.returncode != 0:
        raise RuntimeError(
            f"FFmpeg ошибка (code {proc.returncode}): "
            f"{stderr.decode(errors='replace')[-500:]}"
        )

    logger.info("convert_to_wav: %s → %s", input_path.name, output_path.name)
    return output_path


async def get_duration(file_path: Path) -> int:
    """
    Возвращает длительность аудиофайла в миллисекундах через ffprobe.
    """
    cmd = [
        _ffprobe_bin(),
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        str(file_path),
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()

    if proc.returncode != 0:
        logger.warning("get_duration failed for %s", file_path)
        return 0

    try:
        data = json.loads(stdout)
        duration_sec = float(data["format"]["duration"])
        return int(duration_sec * 1000)
    except (KeyError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("get_duration parse error: %s", exc)
        return 0
