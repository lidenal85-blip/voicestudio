"""
Модуль 3: db/init_db.py
Инициализация БД и dependency get_db для FastAPI.
"""

from __future__ import annotations

import logging
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config.settings import get_settings
from db.models import Base

logger = logging.getLogger(__name__)

_engine = None
_session_factory = None


def _get_engine():
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.DB_URL,
            echo=settings.DEBUG,
            connect_args={"check_same_thread": False},
        )
    return _engine


def _get_session_factory():
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=_get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _session_factory


async def init_db() -> None:
    """Создаёт все таблицы если не существуют. Вызывается при старте."""
    engine = _get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("DB initialized: %s", get_settings().DB_PATH)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency — открывает сессию на время запроса."""
    async with _get_session_factory()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
