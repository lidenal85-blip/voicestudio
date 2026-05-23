"""
services/lrc.py
Генератор LRC-файлов из Whisper-сегментов.
LRC формат: [MM:SS.xx]текст строки
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class LrcLine:
    index: int
    text: str
    start_ms: int
    end_ms: int


def _ms_to_lrc_tag(ms: int) -> str:
    """1234 → [00:01.23]"""
    total_sec = ms / 1000
    minutes   = int(total_sec // 60)
    seconds   = total_sec % 60
    return f"[{minutes:02d}:{seconds:05.2f}]"


def segments_to_lrc(segments: list[dict[str, Any]], title: str = "") -> str:
    """
    Whisper-сегменты → .lrc строка.

    segments: [{"start": 1.0, "end": 3.5, "text": " Текст"}, ...]
              (float секунды, как выдаёт Whisper)
    """
    lines: list[str] = []

    if title:
        lines.append(f"[ti:{title}]")
    lines.append("[by:Voice Studio MVP]")
    lines.append("")

    for seg in segments:
        start_ms = int(float(seg.get("start", 0)) * 1000)
        text     = seg.get("text", "").strip()
        if not text:
            continue
        tag = _ms_to_lrc_tag(start_ms)
        lines.append(f"{tag}{text}")

    return "\n".join(lines)


def segments_to_lrc_lines(
    segments: list[dict[str, Any]],
) -> list[LrcLine]:
    """
    Whisper-сегменты → список LrcLine для записи в БД.
    """
    result: list[LrcLine] = []
    for i, seg in enumerate(segments):
        text = seg.get("text", "").strip()
        if not text:
            continue
        result.append(LrcLine(
            index    = i,
            text     = text,
            start_ms = int(float(seg.get("start", 0)) * 1000),
            end_ms   = int(float(seg.get("end",   0)) * 1000),
        ))
    return result


def parse_lrc(lrc_content: str) -> list[LrcLine]:
    """
    Парсит .lrc текст обратно в список LrcLine.
    Полезно при редактировании и повторном импорте.
    """
    import re
    TAG_RE = re.compile(r"\[(\d{2}):(\d{2}\.\d{2})\](.+)")
    result: list[LrcLine] = []
    lines  = lrc_content.splitlines()
    for i, line in enumerate(lines):
        m = TAG_RE.match(line.strip())
        if not m:
            continue
        minutes, sec_str, text = m.group(1), m.group(2), m.group(3).strip()
        start_ms = int(minutes) * 60_000 + int(float(sec_str) * 1000)
        result.append(LrcLine(index=len(result), text=text, start_ms=start_ms, end_ms=0))

    # Проставляем end_ms = start_ms следующей строки
    for j in range(len(result) - 1):
        result[j].end_ms = result[j + 1].start_ms
    if result:
        result[-1].end_ms = result[-1].start_ms + 3000  # +3с для последней

    return result


def compute_confidence(segments: list[dict[str, Any]]) -> float:
    """Средняя уверенность по всем сегментам (если есть поле avg_logprob)."""
    import math
    probs = [
        math.exp(float(s["avg_logprob"]))
        for s in segments
        if "avg_logprob" in s
    ]
    return round(sum(probs) / len(probs), 4) if probs else 0.0
