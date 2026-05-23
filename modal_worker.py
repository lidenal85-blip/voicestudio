"""
modal_worker.py — Modal GPU воркер для Voice Studio.
Заменяет Colab воркер. Деплоится командой: modal deploy modal_worker.py

Схема работы:
  1. Cron каждые 30 сек → poll_and_process()
  2. GET /api/jobs/pending → список pending jobs
  3. POST /api/jobs/{id}/claim → берём job в работу
  4. Скачиваем original.wav с сервера
  5. Demucs разделяет вокал/инструментал (GPU)
  6. POST /api/jobs/{id}/complete → отдаём файлы
  7. Для transcription jobs → Whisper транскрибирует вокал
"""
import modal

# ── Образ с нужными зависимостями ─────────────────────────────
image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg")
    .pip_install(
        "demucs",
        "openai-whisper",
        "torch",
        "torchaudio",
        "httpx",
        "aiofiles",
    )
)

app = modal.App("voicestudio-worker", image=image)

# ── Секреты из Modal (modal secret create voicestudio-secrets) ─
secrets = [modal.Secret.from_name("voicestudio-secrets")]


# ══════════════════════════════════════════════════════════════
# Вспомогательные функции
# ══════════════════════════════════════════════════════════════

def get_settings():
    """Читаем настройки из переменных окружения."""
    import os
    return {
        "studio_url": os.environ["STUDIO_URL"].rstrip("/"),
        "secret":     os.environ["COLAB_SECRET"],
    }


async def fetch_pending_jobs(client, studio_url: str, secret: str) -> list:
    """Получаем список pending jobs с сервера."""
    r = await client.get(
        f"{studio_url}/api/jobs/pending",
        params={"secret": secret},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


async def claim_job(client, studio_url: str, secret: str, job_id: str) -> dict:
    """Берём job в работу, получаем download_url."""
    r = await client.post(
        f"{studio_url}/api/jobs/{job_id}/claim",
        params={"secret": secret},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


async def download_file(client, url: str, dest_path: str) -> None:
    """Скачиваем файл с сервера."""
    async with client.stream("GET", url, timeout=300) as r:
        r.raise_for_status()
        with open(dest_path, "wb") as f:
            async for chunk in r.aiter_bytes(65536):
                f.write(chunk)


async def complete_separation(
    client, studio_url: str, secret: str,
    job_id: str, vocal_path: str, inst_path: str
) -> dict:
    """Отправляем результаты разделения на сервер."""
    with open(vocal_path, "rb") as vf, open(inst_path, "rb") as inf:
        r = await client.post(
            f"{studio_url}/api/jobs/{job_id}/complete",
            params={"secret": secret},
            files={
                "vocal_file":        ("vocal.wav", vf, "audio/wav"),
                "instrumental_file": ("instrumental.wav", inf, "audio/wav"),
            },
            timeout=300,
        )
    r.raise_for_status()
    return r.json()


async def complete_transcription(
    client, studio_url: str, secret: str,
    job_id: str, segments: list, language: str
) -> dict:
    """
    Отправляем результат транскрибации.
    API ожидает segments_json (список сегментов Whisper с таймкодами).
    Endpoint: /complete-transcription (дефис, не underscore!)
    """
    import json
    r = await client.post(
        f"{studio_url}/api/jobs/{job_id}/complete-transcription",
        params={"secret": secret},
        json={
            "segments_json": json.dumps(segments),
            "language": language,
            "model_used": "base",
        },
        timeout=60,
    )
    r.raise_for_status()
    return r.json()


# ══════════════════════════════════════════════════════════════
# Demucs: разделение вокала и инструментала
# ══════════════════════════════════════════════════════════════

def run_demucs(input_path: str, work_dir: str) -> tuple[str, str]:
    """
    Запускаем Demucs htdemucs.
    Возвращает (vocal_path, instrumental_path).
    """
    import subprocess, os
    from pathlib import Path

    print(f"🎵 Demucs: обрабатываем {input_path}")

    cmd = [
        "python", "-m", "demucs",
        "--two-stems", "vocals",       # только vocals + no_vocals
        "-n", "htdemucs",              # модель
        "-o", work_dir,                # выходная папка
        "--mp3",                       # экономим место
        "--mp3-bitrate", "192",
        input_path,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stderr[-1000:])
        raise RuntimeError(f"Demucs failed: {result.stderr[-300:]}")

    # Demucs создаёт: {work_dir}/htdemucs/{track_name}/vocals.mp3
    track_name = Path(input_path).stem
    out_dir = Path(work_dir) / "htdemucs" / track_name

    vocal_path = str(out_dir / "vocals.mp3")
    inst_path  = str(out_dir / "no_vocals.mp3")

    if not os.path.exists(vocal_path):
        raise RuntimeError(f"Vocal файл не найден: {vocal_path}")
    if not os.path.exists(inst_path):
        raise RuntimeError(f"Instrumental файл не найден: {inst_path}")

    print(f"✅ Demucs готово: vocal={vocal_path}, inst={inst_path}")
    return vocal_path, inst_path


# ══════════════════════════════════════════════════════════════
# Whisper: транскрибация вокала
# ══════════════════════════════════════════════════════════════

def run_whisper(vocal_path: str) -> tuple[list, str]:
    """
    Транскрибируем вокал через Whisper.
    Возвращает (segments, language) — segments с таймкодами для LRC.
    """
    import whisper

    print(f"📝 Whisper: транскрибируем {vocal_path}")
    model = whisper.load_model("base")
    result = model.transcribe(vocal_path, language=None, word_timestamps=False)

    language = result.get("language", "unknown")
    # Конвертируем секунды в миллисекунды для API
    segments = [
        {
            "start": seg["start"],
            "end":   seg["end"],
            "text":  seg["text"].strip(),
        }
        for seg in result.get("segments", [])
    ]

    total_chars = sum(len(s["text"]) for s in segments)
    print(f"✅ Whisper: язык={language}, сегментов={len(segments)}, символов={total_chars}")
    return segments, language


# ══════════════════════════════════════════════════════════════
# Основная функция обработки одного job
# ══════════════════════════════════════════════════════════════

async def process_job(client, settings: dict, job: dict) -> None:
    """Обрабатываем один job (separation или transcription)."""
    import tempfile, os

    job_id    = job["job_id"]
    job_type  = job["type"]
    studio_url = settings["studio_url"]
    secret     = settings["secret"]

    print(f"\n{'='*50}")
    print(f"🔧 Job: {job_id} | Тип: {job_type}")

    # Берём job в работу
    claimed = await claim_job(client, studio_url, secret, job_id)
    download_url = claimed["download_url"]
    print(f"📥 Download URL: {download_url}")

    with tempfile.TemporaryDirectory() as tmp:
        if job_type == "separation":
            # Скачиваем оригинал
            input_path = os.path.join(tmp, "original.wav")
            await download_file(client, download_url, input_path)
            size_mb = os.path.getsize(input_path) / 1024 / 1024
            print(f"📁 Скачано: {size_mb:.1f} MB")

            # Demucs
            vocal_path, inst_path = run_demucs(input_path, tmp)

            # Отправляем результат
            result = await complete_separation(
                client, studio_url, secret,
                job_id, vocal_path, inst_path
            )
            print(f"✅ Разделение завершено: {result}")

        elif job_type == "transcription":
            # Скачиваем вокал
            vocal_path = os.path.join(tmp, "vocal.wav")
            await download_file(client, download_url, vocal_path)

            # Whisper → segments с таймкодами
            segments, language = run_whisper(vocal_path)

            # Отправляем результат
            result = await complete_transcription(
                client, studio_url, secret,
                job_id, segments, language
            )
            print(f"✅ Транскрибация завершена: {result}")

        else:
            print(f"⚠️  Неизвестный тип job: {job_type}")


# ══════════════════════════════════════════════════════════════
# Modal функции
# ══════════════════════════════════════════════════════════════

@app.function(
    gpu="T4",                    # GPU для Demucs и Whisper
    timeout=600,                 # 10 минут максимум на job
    secrets=secrets,
    memory=4096,
)
async def process_single_job(job: dict) -> str:
    """Обрабатываем один job на GPU."""
    import httpx

    settings = get_settings()
    async with httpx.AsyncClient() as client:
        await process_job(client, settings, job)
    return f"done:{job['job_id']}"


@app.function(
    schedule=modal.Period(seconds=30),   # каждые 30 секунд
    secrets=secrets,
    timeout=60,
)
async def poll_and_process() -> None:
    """
    Cron: опрашиваем сервер каждые 30 сек.
    Запускаем GPU функцию для каждого pending job.
    """
    import httpx

    settings   = get_settings()
    studio_url = settings["studio_url"]
    secret     = settings["secret"]

    print(f"🔍 Polling: {studio_url}/api/jobs/pending")

    async with httpx.AsyncClient() as client:
        try:
            jobs = await fetch_pending_jobs(client, studio_url, secret)
        except Exception as e:
            print(f"❌ Ошибка polling: {e}")
            return

    if not jobs:
        print("💤 Нет pending jobs")
        return

    print(f"📋 Найдено jobs: {len(jobs)}")
    for job in jobs:
        print(f"  → {job['job_id']} [{job['type']}]")
        # Запускаем каждый job на отдельном GPU контейнере
        process_single_job.spawn(job)


# ══════════════════════════════════════════════════════════════
# Локальный запуск для тестирования
# ══════════════════════════════════════════════════════════════

@app.local_entrypoint()
def main():
    """
    Запуск: modal run modal_worker.py
    Выполняет один цикл poll_and_process локально.
    """
    print("🚀 VoiceStudio Modal Worker")
    print("Запускаем один цикл обработки...")
    poll_and_process.remote()
    print("✅ Готово")
