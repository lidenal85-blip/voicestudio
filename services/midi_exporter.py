"""
services/midi_exporter.py
Экспорт вокала в MIDI через FFT-детекцию высоты тона.

Алгоритм:
  1. Читаем WAV вокала чанками (frame_ms = 50мс)
  2. Для каждого чанка: FFT → доминирующая частота → MIDI-нота
  3. Группируем соседние одинаковые ноты → MIDI-события
  4. Пишем .mid файл (pure Python, без зависимостей)

Точность: ±1-2 полутона. Достаточно для ориентира мелодии.
"""

from __future__ import annotations

import asyncio
import logging
import math
import struct
import wave
from pathlib import Path

logger = logging.getLogger(__name__)

FRAME_MS     = 50       # мс на фрейм
MIN_FREQ     = 80       # нижняя граница (бас)
MAX_FREQ     = 1200     # верхняя граница (сопрано)
MIN_ENERGY   = 0.001    # порог тишины
MIDI_TEMPO   = 500_000  # 120 BPM в микросекундах на бит
TICKS_PER_BEAT = 480


# ── Pitch detection ────────────────────────────────────────────────────────────

def _freq_to_midi(freq: float) -> int:
    """Частота (Гц) → MIDI нота (0-127). A4=440Hz = 69."""
    if freq <= 0:
        return 0
    note = 69 + 12 * math.log2(freq / 440.0)
    return max(0, min(127, round(note)))


def _detect_pitch(samples: list[float], sample_rate: int) -> float:
    """
    Автокорреляция для определения основного тона.
    Возвращает частоту в Гц или 0 если тихо/атональный звук.
    """
    n = len(samples)
    if n == 0:
        return 0.0

    # Энергия фрейма
    energy = sum(s * s for s in samples) / n
    if energy < MIN_ENERGY:
        return 0.0

    # Диапазон лагов для [MIN_FREQ, MAX_FREQ]
    min_lag = int(sample_rate / MAX_FREQ)
    max_lag = int(sample_rate / MIN_FREQ)
    max_lag = min(max_lag, n // 2)

    if min_lag >= max_lag:
        return 0.0

    # Автокорреляция
    best_lag, best_corr = 0, -1.0
    for lag in range(min_lag, max_lag):
        corr = sum(samples[i] * samples[i + lag] for i in range(n - lag))
        if corr > best_corr:
            best_corr, best_lag = corr, lag

    # Нормализуем корреляцию
    norm = best_corr / (energy * (n - best_lag) + 1e-10)
    if norm < 0.3:   # недостаточно тональный звук
        return 0.0

    return sample_rate / best_lag if best_lag > 0 else 0.0


# ── WAV reading ────────────────────────────────────────────────────────────────

def _read_wav_mono(path: Path) -> tuple[list[float], int]:
    """Читает WAV, возвращает (samples_float, sample_rate)."""
    with wave.open(str(path), "rb") as wf:
        n_channels  = wf.getnchannels()
        sample_rate = wf.getframerate()
        sample_width = wf.getsampwidth()
        n_frames    = wf.getnframes()
        raw         = wf.readframes(n_frames)

    # Декодируем
    if sample_width == 2:
        fmt = f"<{len(raw)//2}h"
        int_samples = list(struct.unpack(fmt, raw))
        scale = 32768.0
    elif sample_width == 1:
        int_samples = [b - 128 for b in raw]
        scale = 128.0
    else:
        raise ValueError(f"Неподдерживаемая разрядность: {sample_width * 8}bit")

    # Смешиваем стерео → моно
    if n_channels == 2:
        mono = [(int_samples[i] + int_samples[i+1]) / 2 for i in range(0, len(int_samples), 2)]
    else:
        mono = int_samples

    return [s / scale for s in mono], sample_rate


# ── MIDI writer (pure Python) ──────────────────────────────────────────────────

def _var_len(value: int) -> bytes:
    """Кодирует целое число в MIDI variable-length."""
    result = []
    result.append(value & 0x7F)
    value >>= 7
    while value:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    return bytes(reversed(result))


def _make_midi(note_events: list[tuple[int, int, int]]) -> bytes:
    """
    note_events: [(midi_note, start_tick, end_tick), ...]
    Возвращает байты MIDI-файла.
    """
    # Формируем события: (tick, type, note, velocity)
    events: list[tuple[int, bytes]] = []

    # Tempo
    events.append((0, b"\xFF\x51\x03" + struct.pack(">I", MIDI_TEMPO)[1:]))

    for note, start, end in sorted(note_events, key=lambda x: x[1]):
        if note <= 0 or end <= start:
            continue
        events.append((start, bytes([0x90, note, 80])))   # Note On  ch1
        events.append((end,   bytes([0x80, note, 0])))    # Note Off ch1

    # Сортируем по времени
    events.sort(key=lambda e: e[0])

    # Кодируем в дельта-тики
    track_data = bytearray()
    last_tick  = 0
    for tick, msg in events:
        delta = tick - last_tick
        last_tick = tick
        track_data += _var_len(delta)
        track_data += msg

    # End of Track
    track_data += b"\x00\xFF\x2F\x00"

    # MIDI Header + Track
    header = struct.pack(">4sHHHH", b"MThd", 6, 0, 1, TICKS_PER_BEAT)
    track_len = struct.pack(">I", len(track_data))
    return header + b"MTrk" + track_len + bytes(track_data)


# ── Main export function ───────────────────────────────────────────────────────

async def vocal_to_midi(vocal_path: Path, output_path: Path) -> Path:
    """
    Анализирует вокал и создаёт MIDI-файл.
    Запускается в asyncio executor чтобы не блокировать event loop.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _vocal_to_midi_sync, vocal_path, output_path)


def _vocal_to_midi_sync(vocal_path: Path, output_path: Path) -> Path:
    logger.info("midi_export: reading %s", vocal_path.name)

    samples, sample_rate = _read_wav_mono(vocal_path)
    frame_size = int(sample_rate * FRAME_MS / 1000)
    ticks_per_frame = int(TICKS_PER_BEAT * FRAME_MS / (MIDI_TEMPO / 1000))

    note_events: list[tuple[int, int, int]] = []
    current_note = 0
    note_start   = 0
    frame_idx    = 0

    for i in range(0, len(samples) - frame_size, frame_size):
        frame = samples[i:i + frame_size]
        freq  = _detect_pitch(frame, sample_rate)
        note  = _freq_to_midi(freq) if freq > 0 else 0

        tick = frame_idx * ticks_per_frame

        if note != current_note:
            if current_note > 0:
                note_events.append((current_note, note_start, tick))
            current_note = note
            note_start   = tick

        frame_idx += 1

    # Закрываем последнюю ноту
    if current_note > 0:
        note_events.append((current_note, note_start, frame_idx * ticks_per_frame))

    midi_bytes = _make_midi(note_events)
    output_path.write_bytes(midi_bytes)

    logger.info(
        "midi_export: %s → %s (%d notes, %.1fKB)",
        vocal_path.name, output_path.name, len(note_events), len(midi_bytes) / 1024,
    )
    return output_path
