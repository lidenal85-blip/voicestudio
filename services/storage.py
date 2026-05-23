"""
Модуль 4: services/storage.py
Абстракция StorageAdapter + LocalStorageAdapter.
"""

from __future__ import annotations

import logging
import shutil
from abc import ABC, abstractmethod
from pathlib import Path

import aiofiles
from fastapi import UploadFile

from config.settings import get_settings

logger = logging.getLogger(__name__)


class StorageAdapter(ABC):
    """Абстрактный интерфейс хранилища."""

    @abstractmethod
    async def put(self, project_id: str, file: UploadFile, filename: str) -> Path:
        """Сохраняет файл. Возвращает абсолютный путь."""

    @abstractmethod
    def get_path(self, project_id: str, filename: str) -> Path:
        """Возвращает путь к файлу (без проверки существования)."""

    @abstractmethod
    async def put_bytes(self, project_id: str, data: bytes, filename: str) -> Path:
        """Сохраняет байты напрямую (для результатов воркера)."""

    @abstractmethod
    def delete(self, project_id: str) -> None:
        """Удаляет всю папку проекта."""


class LocalStorageAdapter(StorageAdapter):
    """Хранилище на локальной ФС — замена S3 для Termux."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    def _project_dir(self, project_id: str) -> Path:
        d = self._root / project_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    async def put(self, project_id: str, file: UploadFile, filename: str) -> Path:
        dest = self._project_dir(project_id) / filename
        settings = get_settings()
        written = 0
        async with aiofiles.open(dest, "wb") as out:
            while chunk := await file.read(1024 * 256):  # 256 KB chunks
                written += len(chunk)
                if written > settings.MAX_FILE_SIZE_BYTES:
                    raise ValueError(
                        f"Файл превышает {settings.MAX_FILE_SIZE_MB} MB"
                    )
                await out.write(chunk)
        logger.info("put: %s (%d bytes)", dest, written)
        return dest

    async def put_bytes(self, project_id: str, data: bytes, filename: str) -> Path:
        dest = self._project_dir(project_id) / filename
        async with aiofiles.open(dest, "wb") as out:
            await out.write(data)
        logger.info("put_bytes: %s (%d bytes)", dest, len(data))
        return dest

    def get_path(self, project_id: str, filename: str) -> Path:
        return self._root / project_id / filename

    def delete(self, project_id: str) -> None:
        target = self._root / project_id
        if target.exists():
            shutil.rmtree(target)
            logger.info("delete: removed %s", target)


def get_storage() -> LocalStorageAdapter:
    """FastAPI dependency."""
    return LocalStorageAdapter(root=get_settings().STORAGE_PATH)
