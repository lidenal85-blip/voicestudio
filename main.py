"""
main.py — FastAPI приложение Voice Studio MVP.
FIX BUG-6: scheduler task с авто-рестартом (не умирает молча).
"""
from __future__ import annotations
import asyncio, logging, logging.handlers, sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config.settings import get_settings
from db.init_db import init_db


def _setup_logging(log_path: Path, debug: bool) -> None:
    level = logging.DEBUG if debug else logging.INFO
    fmt   = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        logging.handlers.RotatingFileHandler(
            log_path, maxBytes=5*1024*1024, backupCount=3, encoding="utf-8"
        ),
    ]
    logging.basicConfig(level=level, format=fmt, handlers=handlers)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


# BUG-6 FIX: scheduler с авто-рестартом
async def _run_scheduler_resilient(storage_path: Path) -> None:
    """Обёртка над run_scheduler с авто-рестартом при падении."""
    logger = logging.getLogger(__name__)
    from infrastructure.scheduler import run_scheduler
    while True:
        try:
            await run_scheduler(storage_path)
        except asyncio.CancelledError:
            logger.info("scheduler: cancelled, stopping")
            break
        except Exception as exc:
            logger.error("scheduler crashed, restarting in 15s: %s", exc)
            await asyncio.sleep(15)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    _setup_logging(settings.LOG_PATH, settings.DEBUG)
    logger = logging.getLogger(__name__)

    logger.info("=== Voice Studio MVP starting ===")
    logger.info("DB:      %s", settings.DB_PATH)
    logger.info("Storage: %s", settings.STORAGE_PATH)

    await init_db()
    logger.info("DB ready")

    from infrastructure.scheduler import recover_on_startup
    recovered = await recover_on_startup()
    if recovered:
        logger.info("Startup recovery: %d jobs recreated", recovered)

    scheduler_task = asyncio.create_task(_run_scheduler_resilient(settings.STORAGE_PATH))
    logger.info("Scheduler started (with auto-restart)")

    yield

    scheduler_task.cancel()
    try:
        await scheduler_task
    except asyncio.CancelledError:
        pass
    logger.info("=== Voice Studio MVP shutdown ===")


def create_app() -> FastAPI:
    settings = get_settings()

    import os
    root_path = os.environ.get("ROOT_PATH", "")

    app = FastAPI(
        title="Voice Studio MVP",
        description="AI-сервис разделения аудио на вокал и инструментал",
        version="1.1.0",
        lifespan=lifespan,
        root_path=root_path,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_origin_regex=r"https://.+\.(loca\.lt|trycloudflare\.com|railway\.app|up\.railway\.app)",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    from web.router import router as web_router
    from api.routers import router as api_router
    from api.download import router as download_router
    from api.transcription import router as transcription_router
    from api.recording import router as recording_router
    from api.export import router as export_router
    from api.reprocess import router as reprocess_router
    from api.system import router as system_router
    from infrastructure.ws import router as ws_router
    from fastapi.staticfiles import StaticFiles

    static_dir = Path(__file__).parent / "web" / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    app.include_router(web_router)
    app.include_router(api_router,           prefix="/api")
    app.include_router(download_router,       prefix="/api")
    app.include_router(transcription_router, prefix="/api")
    app.include_router(recording_router,     prefix="/api")
    app.include_router(export_router,        prefix="/api")
    app.include_router(reprocess_router,     prefix="/api")
    app.include_router(system_router)
    app.include_router(ws_router)

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn
    s = get_settings()
    uvicorn.run("main:app", host=s.HOST, port=s.PORT,
                reload=s.DEBUG, log_level="debug" if s.DEBUG else "info")
