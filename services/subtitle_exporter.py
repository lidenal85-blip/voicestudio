"""
services/subtitle_exporter.py
Конвертация LRC → SRT и WebVTT субтитры.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from db.models import LyricLine


def _ms_to_srt_time(ms: int) -> str:
    """1234 → 00:00:01,234"""
    h, r  = divmod(ms, 3_600_000)
    m, r  = divmod(r, 60_000)
    s, ms = divmod(r, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _ms_to_vtt_time(ms: int) -> str:
    """1234 → 00:00:01.234"""
    return _ms_to_srt_time(ms).replace(",", ".")


def lines_to_srt(lines: list) -> str:
    """
    LyricLine list → SRT строка.
    Формат:
      1
      00:00:01,000 --> 00:00:03,500
      Текст строки
    """
    parts: list[str] = []
    for i, ln in enumerate(lines, start=1):
        parts.append(
            f"{i}\n"
            f"{_ms_to_srt_time(ln.start_ms)} --> {_ms_to_srt_time(ln.end_ms)}\n"
            f"{ln.text}\n"
        )
    return "\n".join(parts)


def lines_to_vtt(lines: list, title: str = "") -> str:
    """LyricLine list → WebVTT строка."""
    parts = ["WEBVTT", ""]
    if title:
        parts.append(f"NOTE {title}")
        parts.append("")
    for i, ln in enumerate(lines, start=1):
        parts.append(
            f"{i}\n"
            f"{_ms_to_vtt_time(ln.start_ms)} --> {_ms_to_vtt_time(ln.end_ms)}\n"
            f"{ln.text}"
        )
        parts.append("")
    return "\n".join(parts)


def lines_to_lrc(lines: list, title: str = "", artist: str = "") -> str:
    """LyricLine list → LRC строка (для повторного экспорта)."""
    parts: list[str] = []
    if title:  parts.append(f"[ti:{title}]")
    if artist: parts.append(f"[ar:{artist}]")
    parts.append("[by:Voice Studio MVP]")
    parts.append("")
    for ln in lines:
        ms   = ln.start_ms
        mins = ms // 60_000
        secs = (ms % 60_000) / 1000
        parts.append(f"[{mins:02d}:{secs:05.2f}]{ln.text}")
    return "\n".join(parts)
