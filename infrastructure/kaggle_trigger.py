"""
infrastructure/kaggle_trigger.py
Мульти-аккаунтный запуск Kaggle GPU воркера.

Стратегия "первый свободный":
  Перебираем аккаунты по очереди.
  Берём первый у которого ядро не running/queued.
  Если все заняты — ждём следующего цикла.

Конфигурация в .env:
  # Вариант 1: один аккаунт (совместимость)
  KAGGLE_USERNAME=user1
  KAGGLE_KEY=key1

  # Вариант 2: несколько аккаунтов (приоритет)
  KAGGLE_ACCOUNTS=user1:key1,user2:key2,user3:key3

  # Можно комбинировать — KAGGLE_ACCOUNTS дополняет основной аккаунт
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_BUSY_STATUSES  = {"running", "queued"}
_trigger_lock   = asyncio.Lock()


@dataclass
class KaggleAccount:
    username: str
    key: str
    kernel_slug: str = "voice-studio-worker"

    @property
    def kernel_id(self) -> str:
        return f"{self.username}/{self.kernel_slug}"


def _make_env(account: KaggleAccount) -> dict:
    """
    Формирует env для Kaggle CLI.
    Поддерживает оба формата:
      Старый: KAGGLE_USERNAME + KAGGLE_KEY (из kaggle.json)
      Новый:  KAGGLE_TOKEN=KGAT_xxx (Kaggle v1.6+)
    """
    base = dict(os.environ)
    if account.key.startswith("KGAT_"):
        # Новый формат — токен
        base["KAGGLE_TOKEN"] = account.key
        # Username всё равно нужен для kernel_id
        base["KAGGLE_USERNAME"] = account.username
    else:
        # Старый формат — username + api_key
        base["KAGGLE_USERNAME"] = account.username
        base["KAGGLE_KEY"]      = account.key
    return base


def parse_accounts(settings) -> list[KaggleAccount]:
    """
    Собирает список аккаунтов из настроек.
    Порядок: KAGGLE_ACCOUNTS → KAGGLE_USERNAME/KEY (как fallback).
    """
    accounts: list[KaggleAccount] = []
    slug = settings.KAGGLE_KERNEL_SLUG or "voice-studio-worker"

    # Из KAGGLE_ACCOUNTS="user1:key1,user2:key2"
    raw = getattr(settings, "KAGGLE_ACCOUNTS", "").strip()
    if raw:
        for pair in raw.split(","):
            pair = pair.strip()
            if ":" not in pair:
                continue
            user, key = pair.split(":", 1)
            user = user.strip(); key = key.strip()
            if user and key:
                accounts.append(KaggleAccount(username=user, key=key, kernel_slug=slug))

    # Основной аккаунт (если не дублирует)
    main_user = getattr(settings, "KAGGLE_USERNAME", "").strip()
    main_key  = getattr(settings, "KAGGLE_KEY",      "").strip()
    if main_user and main_key:
        existing = {a.username for a in accounts}
        if main_user not in existing:
            accounts.insert(0, KaggleAccount(username=main_user, key=main_key, kernel_slug=slug))

    return accounts


async def get_kernel_status(account: KaggleAccount) -> str | None:
    """
    Возвращает статус последнего запуска ядра аккаунта.
    None = ядро не существует или ошибка API.
    """
    env = _make_env(account)
    try:
        proc = await asyncio.create_subprocess_exec(
            "kaggle", "kernels", "status", account.kernel_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        output = stdout.decode().strip().lower()
        for s in ("running", "queued", "complete", "error", "cancel"):
            if s in output:
                return s
        return None
    except Exception as e:
        logger.debug("get_kernel_status %s: %s", account.kernel_id, e)
        return None


async def find_free_account(accounts: list[KaggleAccount]) -> KaggleAccount | None:
    """
    Параллельно проверяет статусы всех аккаунтов.
    Возвращает первый свободный (не running/queued).
    """
    if not accounts:
        return None

    statuses = await asyncio.gather(
        *[get_kernel_status(acc) for acc in accounts],
        return_exceptions=True,
    )

    busy_count = 0
    for acc, status in zip(accounts, statuses):
        if isinstance(status, Exception):
            status = None
        is_busy = status in _BUSY_STATUSES
        logger.debug(
            "account %s: %s %s",
            acc.username, status or "unknown", "← BUSY" if is_busy else "← FREE"
        )
        if not is_busy:
            return acc
        busy_count += 1

    logger.info("all %d kaggle accounts busy", busy_count)
    return None


def _build_worker_script(studio_url: str, colab_secret: str,
                          demucs_model: str = "htdemucs",
                          whisper_model: str = "base") -> str:
    """Генерирует Python-скрипт для запуска на Kaggle."""
    return f'''#!/usr/bin/env python3
"""Voice Studio Worker — Kaggle. Auto-generated."""
import subprocess, sys, time, json, shutil, traceback, os
from pathlib import Path

subprocess.run("pip install -q demucs openai-whisper", shell=True, check=True)

import torch, requests, whisper

STUDIO_URL    = "{studio_url}"
SECRET        = "{colab_secret}"
DEMUCS_MODEL  = "{demucs_model}"
WHISPER_MODEL = "{whisper_model}"
WORK_DIR      = "/kaggle/working/vs_work"
DEVICE        = "cuda" if torch.cuda.is_available() else "cpu"
UPLOAD_BITRATE= "192k"
POLL_INTERVAL = 8
MAX_EMPTY     = 3

os.makedirs(WORK_DIR, exist_ok=True)
HEADERS = {{"bypass-tunnel-reminder":"true","User-Agent":"VoiceStudio-Kaggle/1.0"}}

print(f"Device: {{DEVICE}} | Studio: {{STUDIO_URL}}")

_W = {{}}
def get_whisper(name):
    if name not in _W:
        _W[name] = whisper.load_model(name, device=DEVICE)
        print(f"Whisper {{name}} ready")
    return _W[name]
get_whisper(WHISPER_MODEL)

def api(method, path, **kwargs):
    url = STUDIO_URL + path
    headers = {{**HEADERS, **kwargs.pop("headers", {{}})}}
    kwargs.setdefault("timeout", 30)
    r = requests.request(method, url, headers=headers, **kwargs)
    if not r.ok:
        try:    detail = r.json()
        except: detail = r.text[:200]
        raise Exception(f"{{r.status_code}}: {{detail}}")
    return r

def log(msg, lv="INFO"):
    icons = {{"INFO":"ℹ️","OK":"✓","ERR":"✗","WARN":"⚠️"}}
    print(f"[{{time.strftime('%H:%M:%S')}}] {{icons.get(lv,'')}} {{msg}}")

def download(url, dest):
    r = requests.get(url, headers=HEADERS, stream=True, timeout=180)
    r.raise_for_status()
    with open(dest,"wb") as f:
        for chunk in r.iter_content(256*1024): f.write(chunk)
    log(f"Downloaded {{Path(dest).name}} {{Path(dest).stat().st_size/1048576:.1f}}MB","OK")

def to_mp3(wav):
    mp3 = Path(wav).with_suffix(".mp3")
    subprocess.run(f\'ffmpeg -y -i "{{wav}}" -b:a {{UPLOAD_BITRATE}} "{{mp3}}" -loglevel error\',shell=True)
    if mp3.exists():
        w,m = Path(wav).stat().st_size, mp3.stat().st_size
        log(f"{{Path(wav).name}}: {{w/1048576:.1f}}MB→{{m/1048576:.1f}}MB MP3","OK")
        return str(mp3),"audio/mpeg"
    return str(wav),"audio/wav"

def run_demucs(inp, job_dir, model=None):
    model = model or DEMUCS_MODEL
    out   = Path(job_dir)/"sep"; out.mkdir(exist_ok=True)
    log(f"Demucs ({{model}})...")
    t0 = time.time()
    r  = subprocess.run(
        f\'python -m demucs --name "{{model}}" --two-stems vocals --out "{{out}}" "{{inp}}"\',
        shell=True, capture_output=True, text=True)
    if r.returncode != 0: raise RuntimeError(r.stderr[-400:])
    log(f"Demucs {{time.time()-t0:.1f}}s","OK")
    vocals  = list(out.rglob("vocals.wav"))
    novoice = list(out.rglob("no_vocals.wav"))
    if not vocals: raise FileNotFoundError("Demucs no output")
    v = Path(job_dir)/"vocal.wav";  shutil.copy2(vocals[0], v)
    i = Path(job_dir)/"instru.wav"; shutil.copy2(novoice[0], i)
    return v, i

def upload_separation(jid, v, i):
    vf,vt = to_mp3(v); if_,it = to_mp3(i)
    log(f"Uploading {{Path(vf).stat().st_size/1048576:.1f}}+{{Path(if_).stat().st_size/1048576:.1f}}MB...")
    with open(vf,"rb") as a, open(if_,"rb") as b:
        api("POST", f"/api/jobs/{{jid}}/complete?secret={{SECRET}}",
            files={{"vocal_file":(Path(vf).name,a,vt),
                   "instrumental_file":(Path(if_).name,b,it)}}, timeout=120)
    log("Separation uploaded","OK")

def run_whisper(path, model_name=None):
    model_name = model_name or WHISPER_MODEL
    log(f"Whisper ({{model_name}})...")
    t0  = time.time()
    res = get_whisper(model_name).transcribe(str(path), task="transcribe", verbose=False)
    log(f"Whisper {{time.time()-t0:.1f}}s lang={{res.get('language')}} segs={{len(res.get('segments',[]))}}","OK")
    return res

def upload_transcription(jid, res):
    segs = [{{"id":s.get("id"),"start":round(float(s.get("start",0)),3),
              "end":round(float(s.get("end",0)),3),"text":s.get("text","").strip(),
              "avg_logprob":round(float(s.get("avg_logprob",0)),4)}}
            for s in res.get("segments",[]) if s.get("text","").strip()]
    body = {{"segments_json":json.dumps(segs,ensure_ascii=False),
             "language":res.get("language","ru"),"model_used":WHISPER_MODEL}}
    d = api("POST",f"/api/jobs/{{jid}}/complete-transcription?secret={{SECRET}}",
            json=body,timeout=60).json()
    log(f"Transcription lines={{d.get('lines')}}","OK")

def process_separation(job):
    jid=job["job_id"]; pid=job["project_id"]
    d=Path(WORK_DIR)/jid; d.mkdir(exist_ok=True)
    log(f"═ SEPARATION {{jid[:8]}}")
    try:
        api("POST",f"/api/jobs/{{jid}}/claim?secret={{SECRET}}")
        download(f"{{STUDIO_URL}}/api/projects/{{pid}}/assets/original", d/"original.wav")
        m = job.get("input_params",{{}}).get("demucs_model", DEMUCS_MODEL)
        v,i = run_demucs(d/"original.wav", d, model=m)
        upload_separation(jid,v,i)
        log(f"═ SEPARATION {{jid[:8]}} DONE ✓","OK"); return True
    except Exception as e:
        log(f"═ SEPARATION ERROR: {{e}}","ERR"); traceback.print_exc(); return False
    finally:
        shutil.rmtree(d, ignore_errors=True)

def process_transcription(job):
    jid=job["job_id"]; pid=job["project_id"]
    d=Path(WORK_DIR)/jid; d.mkdir(exist_ok=True)
    log(f"═ TRANSCRIPTION {{jid[:8]}}")
    try:
        api("POST",f"/api/jobs/{{jid}}/claim?secret={{SECRET}}")
        download(f"{{STUDIO_URL}}/api/projects/{{pid}}/assets/vocal", d/"vocal.wav")
        m   = job.get("input_params",{{}}).get("whisper_model", WHISPER_MODEL)
        res = run_whisper(d/"vocal.wav", model_name=m)
        upload_transcription(jid, res)
        log(f"═ TRANSCRIPTION {{jid[:8]}} DONE ✓","OK"); return True
    except Exception as e:
        log(f"═ TRANSCRIPTION ERROR: {{e}}","ERR"); traceback.print_exc(); return False
    finally:
        shutil.rmtree(d, ignore_errors=True)

def process_job(job):
    t = job.get("type")
    if t=="separation":    return process_separation(job)
    if t=="transcription": return process_transcription(job)
    log(f"Unknown: {{t}}","WARN"); return False

# ── Основной цикл ─────────────────────────────────────────────
log(f"Kaggle worker started | Demucs={{DEMUCS_MODEL}} | Whisper={{WHISPER_MODEL}}")
print("="*50)

empty_streak=0; done=0; failed=0
while True:
    try:
        jobs = api("GET",f"/api/jobs/pending?secret={{SECRET}}").json()
        if not jobs:
            empty_streak += 1
            print(f"[{{time.strftime('%H:%M:%S')}}] empty {{empty_streak}}/{{MAX_EMPTY}}...", end="\\r")
            if empty_streak >= MAX_EMPTY:
                log(f"Queue empty — shutting down. Saved GPU quota!")
                break
        else:
            empty_streak = 0
            log(f"Jobs: {{len(jobs)}}")
            for job in jobs:
                ok = process_job(job)
                done += ok; failed += (not ok)
    except Exception as e:
        log(f"Error: {{e}}","ERR"); empty_streak += 1
        if empty_streak >= MAX_EMPTY: break
    time.sleep(POLL_INTERVAL)

print("="*50)
log(f"Done: processed={{done}} failed={{failed}}")
'''


async def push_kernel(account: KaggleAccount, studio_url: str, colab_secret: str) -> bool:
    """Пушит скрипт на Kaggle-аккаунт и запускает ядро."""
    script   = _build_worker_script(studio_url, colab_secret)
    metadata = {
        "id":              account.kernel_id,
        "title":           "Voice Studio Worker",
        "code_file":       "kaggle_worker.py",
        "language":        "python",
        "kernel_type":     "script",
        "is_private":      True,
        "enable_gpu":      True,
        "enable_internet": True,
    }

    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "kernel-metadata.json").write_text(json.dumps(metadata, indent=2))
        Path(tmp, "kaggle_worker.py").write_text(script)

        env = _make_env(account)
        proc = await asyncio.create_subprocess_exec(
            "kaggle", "kernels", "push", "-p", tmp,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        except asyncio.TimeoutError:
            proc.kill()
            logger.error("push_kernel timeout for %s", account.username)
            return False

    if proc.returncode == 0:
        logger.info("✓ Kaggle kernel pushed: %s", account.kernel_id)
        return True
    else:
        logger.error("push_kernel failed (%s): %s", account.username,
                     stderr.decode(errors="replace")[:300])
        return False


async def trigger_kaggle_worker(settings) -> tuple[bool, str]:
    """
    Точка входа для scheduler.
    Находит свободный аккаунт и запускает на нём воркер.
    Возвращает (success, username).
    """
    async with _trigger_lock:
        accounts = parse_accounts(settings)
        if not accounts:
            logger.debug("no kaggle accounts configured")
            return False, ""

        logger.info(
            "kaggle: checking %d account(s): %s",
            len(accounts), [a.username for a in accounts]
        )

        free = await find_free_account(accounts)
        if free is None:
            logger.info("kaggle: all accounts busy, will retry next cycle")
            return False, ""

        logger.info("kaggle: using account '%s'", free.username)
        ok = await push_kernel(free, settings.base_url, settings.COLAB_SECRET)
        return ok, free.username if ok else ""
