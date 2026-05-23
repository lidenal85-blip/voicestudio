"""
infrastructure/scheduler.py
Фоновые задачи: timeout scanner, startup recovery, Kaggle auto-trigger, Telegram.
FIX BUG-6: run_scheduler не выбрасывает исключения наружу (main.py рестартует через обёртку).
"""
from __future__ import annotations
import asyncio, json, logging, os, shutil, time
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

HARD_TIMEOUTS = {"separation": 15, "transcription": 10, "slicing": 5}
RECOVERY_MAP  = {
    "separating":   "separation",
    "transcribing": "transcription",
    "slicing":      "slicing",
}

_last_kaggle_trigger = 0.0   # module-level для правильного отслеживания


class _Metrics:
    def __init__(self):
        self.started_at         = time.time()
        self.jobs_timed_out     = 0
        self.jobs_recovered     = 0
        self.temp_files_cleaned = 0
        self.storage_bytes_used = 0
        self.last_scan_at       = None
        self.last_cleanup_at    = None

metrics = _Metrics()


async def scan_timeouts() -> int:
    from db.init_db import _get_session_factory
    from db.models import Job, Project
    from sqlalchemy import select

    now   = datetime.now(timezone.utc)
    count = 0

    async with _get_session_factory()() as session:
        result = await session.execute(select(Job).where(Job.status == "processing"))
        for job in result.scalars().all():
            timeout_min = HARD_TIMEOUTS.get(job.type, 15)
            deadline    = job.created_at.replace(tzinfo=timezone.utc) + timedelta(minutes=timeout_min)
            if now > deadline:
                elapsed = (now - job.created_at.replace(tzinfo=timezone.utc)).total_seconds() / 60
                logger.warning("timeout: job=%s type=%s %.1fmin → failed", job.id[:8], job.type, elapsed)
                job.status    = "failed"
                job.error_msg = f"Hard timeout ({timeout_min}min)"
                proj_res      = await session.execute(
                    select(Project).where(Project.id == job.project_id)
                )
                p = proj_res.scalar_one_or_none()
                if p and p.status == "processing":
                    p.status = "failed"
                count += 1
        if count:
            await session.commit()
            metrics.jobs_timed_out += count

    metrics.last_scan_at = now
    return count


async def recover_on_startup() -> int:
    from db.init_db import _get_session_factory
    from db.models import Job, Project
    from sqlalchemy import select

    count = 0
    async with _get_session_factory()() as session:
        result = await session.execute(select(Project).where(Project.status == "processing"))
        for project in result.scalars().all():
            job_type = RECOVERY_MAP.get(project.pipeline_state)
            if not job_type:
                continue
            jobs_res = await session.execute(
                select(Job).where(
                    Job.project_id == project.id,
                    Job.type       == job_type,
                    Job.status.in_(["pending", "processing"]),
                )
            )
            if jobs_res.scalars().all():
                continue
            new_key = f"{project.id}:{job_type}:recovery:{int(time.time())}"
            session.add(Job(
                project_id=project.id, type=job_type, status="pending",
                idempotency_key=new_key,
                input_params=json.dumps({"recovery": True}),
            ))
            count += 1
            logger.info("recovery: project=%s pipeline=%s → new %s job", project.id[:8], project.pipeline_state, job_type)
        if count:
            await session.commit()
            metrics.jobs_recovered += count

    logger.info("startup_recovery: %d jobs recreated", count)
    return count


async def clean_temp_files(storage_path: Path, max_age_hours: int = 1) -> int:
    cleaned = 0
    cutoff  = time.time() - max_age_hours * 3600

    def _clean_dir(d: Path) -> int:
        n = 0
        if not d.exists(): return 0
        for f in d.iterdir():
            try:
                if f.is_file() and f.stat().st_mtime < cutoff:
                    f.unlink(); n += 1
                elif f.is_dir() and f.name == "temp":
                    shutil.rmtree(f, ignore_errors=True); n += 1
            except OSError:
                pass
        return n

    if storage_path.exists():
        for proj_dir in storage_path.iterdir():
            if proj_dir.is_dir():
                cleaned += _clean_dir(proj_dir / "temp")

    tmp = Path("/tmp/voice-studio")
    if tmp.exists():
        cleaned += _clean_dir(tmp)

    if cleaned:
        logger.info("temp_cleaner: removed %d", cleaned)
    metrics.temp_files_cleaned += cleaned
    metrics.last_cleanup_at     = datetime.now(timezone.utc)
    return cleaned


def update_storage_metrics(storage_path: Path) -> int:
    total = 0
    try:
        for root, _, files in os.walk(storage_path):
            for f in files:
                try: total += os.path.getsize(os.path.join(root, f))
                except OSError: pass
    except Exception: pass
    metrics.storage_bytes_used = total
    return total


async def _maybe_trigger_kaggle(cooldown: float = 120) -> None:
    """Запускает Kaggle воркер если есть pending jobs и прошёл cooldown."""
    global _last_kaggle_trigger
    if time.time() - _last_kaggle_trigger < cooldown:
        return

    from config.settings import get_settings
    settings = get_settings()
    if not settings.kaggle_enabled:
        return

    from db.init_db import _get_session_factory
    from db.models import Job
    from sqlalchemy import select

    async with _get_session_factory()() as session:
        res = await session.execute(
            select(Job).where(
                Job.status == "pending",
                Job.type.in_(["separation", "transcription"]),
            ).limit(1)
        )
        pending = res.scalar_one_or_none()

    if not pending:
        return

    logger.info("scheduler: pending jobs found → triggering Kaggle")

    from infrastructure.kaggle_trigger import trigger_kaggle_worker
    from infrastructure.telegram_notify import notify_worker_started

    triggered, username = await trigger_kaggle_worker(settings)

    if triggered:
        _last_kaggle_trigger = time.time()
        await notify_worker_started(settings, source=f"kaggle/{username}")


async def _notify_completed_projects() -> None:
    """Telegram уведомление о готовых проектах (один раз)."""
    from config.settings import get_settings
    settings = get_settings()
    if not settings.telegram_enabled:
        return

    from db.init_db import _get_session_factory
    from db.models import PipelineEvent, Project, Transcription
    from sqlalchemy import select

    async with _get_session_factory()() as session:
        notified_res = await session.execute(
            select(PipelineEvent.project_id)
            .where(PipelineEvent.event_type == "telegram_notified")
        )
        notified_ids = {r[0] for r in notified_res.all()}

        ready_res = await session.execute(
            select(Project).where(
                Project.status == "ready",
                Project.id.notin_(notified_ids) if notified_ids else True,
            ).limit(5)
        )
        projects = ready_res.scalars().all()

        for project in projects:
            tr_res = await session.execute(
                select(Transcription).where(
                    Transcription.project_id == project.id,
                    Transcription.is_active  == True,  # noqa
                )
            )
            tr    = tr_res.scalar_one_or_none()
            stats = {"lines": 0, "confidence": 0}
            if tr:
                stats["lines"]      = len(tr.lrc_content.splitlines()) if tr.lrc_content else 0
                stats["confidence"] = tr.confidence or 0

            from infrastructure.telegram_notify import notify_ready
            await notify_ready(settings, project.title, stats)

            session.add(PipelineEvent(
                project_id=project.id, event_type="telegram_notified",
                data_json=json.dumps({"title": project.title}),
            ))

        if projects:
            await session.commit()


async def run_scheduler(storage_path: Path) -> None:
    """
    Основной цикл. Вызывается из main.py через _run_scheduler_resilient.
    Все исключения логируются, цикл не прерывается (BUG-6 FIX).
    """
    logger.info("scheduler: started")
    scan_interval    = 30
    cleanup_interval = 3600
    last_cleanup     = 0.0

    while True:
        try:
            await scan_timeouts()
            await _maybe_trigger_kaggle(cooldown=120)
            await _notify_completed_projects()
            update_storage_metrics(storage_path)
            if time.time() - last_cleanup > cleanup_interval:
                await clean_temp_files(storage_path)
                last_cleanup = time.time()
        except asyncio.CancelledError:
            logger.info("scheduler: cancelled")
            break
        except Exception as exc:
            # Не падаем — логируем и продолжаем
            logger.error("scheduler loop error (continuing): %s", exc)

        await asyncio.sleep(scan_interval)
