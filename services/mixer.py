"""
services/mixer.py
FFmpeg микширование инструментала + голоса с коррекцией latency.
FIX: убрана дублирующая convert_to_wav, используется из converter.py.
"""
from __future__ import annotations
import asyncio, logging
from pathlib import Path

logger = logging.getLogger(__name__)


async def mix_audio(
    instrumental_path: Path,
    mic_path: Path,
    output_path: Path,
    timing_offset_ms: int = 0,
    instrumental_volume: float = 0.8,
    mic_volume: float = 1.0,
) -> Path:
    """Микширует инструментал и голос с коррекцией latency."""
    if timing_offset_ms > 0:
        delay_str    = f"{timing_offset_ms}|{timing_offset_ms}"
        filter_graph = (
            f"[0]volume={instrumental_volume}[inst];"
            f"[1]adelay={delay_str},volume={mic_volume}[mic];"
            f"[inst][mic]amix=inputs=2:duration=first:dropout_transition=0[out]"
        )
    elif timing_offset_ms < 0:
        trim_sec     = abs(timing_offset_ms) / 1000
        filter_graph = (
            f"[0]volume={instrumental_volume}[inst];"
            f"[1]atrim=start={trim_sec:.4f},asetpts=PTS-STARTPTS,volume={mic_volume}[mic];"
            f"[inst][mic]amix=inputs=2:duration=first:dropout_transition=0[out]"
        )
    else:
        filter_graph = (
            f"[0]volume={instrumental_volume}[inst];"
            f"[1]volume={mic_volume}[mic];"
            f"[inst][mic]amix=inputs=2:duration=first:dropout_transition=0[out]"
        )

    cmd = [
        "ffmpeg", "-y",
        "-i", str(instrumental_path),
        "-i", str(mic_path),
        "-filter_complex", filter_graph,
        "-map", "[out]",
        "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2",
        str(output_path),
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"FFmpeg mix failed: {stderr.decode(errors='replace')[-400:]}")

    logger.info("mix_audio: %s + %s → %s (offset=%dms)",
                instrumental_path.name, mic_path.name, output_path.name, timing_offset_ms)
    return output_path


async def convert_webm_to_wav(input_path: Path, output_path: Path | None = None) -> Path:
    """WebM/Opus → WAV для mic-записей из браузера."""
    from services.converter import convert_to_wav
    return await convert_to_wav(input_path, output_path)
