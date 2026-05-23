"""
Модуль 1: config/settings.py
Pydantic-settings конфиг для AI Voice Studio MVP.
Все значения — из .env, хардкода нет.

Фикс: CORS_ORIGINS хранится как str (pydantic-settings v2 ломается на list[str]
из .env — пытается json.loads("") до запуска валидатора).
Используй property cors_origins_list везде в коде.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Пути
    STORAGE_PATH: Path = Field(
        default=Path("storage"),
        description="Корневая папка для хранения аудиофайлов",
    )
    DB_PATH: Path = Field(
        default=Path("voice_studio.db"),
        description="Путь к SQLite-базе",
    )
    LOG_PATH: Path = Field(
        default=Path("logs/session.log"),
        description="Путь к лог-файлу",
    )

    # Ограничения
    MAX_FILE_SIZE_MB: int = Field(
        default=100,
        ge=1,
        le=500,
        description="Максимальный размер загружаемого файла, MB",
    )

    # Безопасность
    COLAB_SECRET: str = Field(
        ...,
        min_length=8,
        description="Секретный ключ для Colab-воркера",
    )

    # Сервер
    HOST: str = Field(default="0.0.0.0")
    PORT: int = Field(default=8000, ge=1024, le=65535)
    DEBUG: bool = Field(default=False)

    # CORS — СТРОКА, не list[str].
    # pydantic-settings v2 вызывает json.loads() на list-полях ДО валидатора,
    # что ломается при пустом или comma-separated значении из .env.
    # Используй property cors_origins_list для получения списка.
    CORS_ORIGINS: str = Field(
        default="http://localhost:8000,http://127.0.0.1:8000",
        description="CORS origins через запятую или JSON-массив",
    )

    # Публичный URL — заполняется автоматически:
    PUBLIC_URL: str = Field(default="", description="Публичный URL сервера.")

    # ── Kaggle auto-trigger ────────────────────────────────────────────────────
    KAGGLE_USERNAME:    str = Field(default="", description="Kaggle username")
    KAGGLE_KEY:         str = Field(default="", description="Kaggle API key")
    # Несколько аккаунтов через запятую: "user1:key1,user2:key2"
    # Если задан — используется вместо/вместе с KAGGLE_USERNAME/KEY
    KAGGLE_ACCOUNTS: str = Field(
        default="",
        description="Мульти-аккаунт Kaggle: user1:key1,user2:key2,...",
    )
    KAGGLE_KERNEL_SLUG: str = Field(
        default="voice-studio-worker",
        description="Slug ядра на Kaggle (создаётся один раз)"
    )

    # ── Telegram уведомления ──────────────────────────────────────────────────
    TELEGRAM_BOT_TOKEN: str = Field(default="", description="BotFather token")
    TELEGRAM_CHAT_ID:   str = Field(default="", description="Твой chat_id")

    @property
    def kaggle_enabled(self) -> bool:
        return bool(self.KAGGLE_USERNAME and self.KAGGLE_KEY)

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.TELEGRAM_BOT_TOKEN and self.TELEGRAM_CHAT_ID)

    @property
    def base_url(self) -> str:
        """
        Публичный URL для download_url в /api/jobs/claim.
        Приоритет: PUBLIC_URL → RAILWAY_PUBLIC_DOMAIN → localhost.
        """
        if self.PUBLIC_URL:
            return self.PUBLIC_URL.rstrip("/")
        import os
        railway = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
        if railway:
            return f"https://{railway}"
        return f"http://localhost:{self.PORT}"

    # ── Properties ─────────────────────────────────────────────────────────────

    @property
    def MAX_FILE_SIZE_BYTES(self) -> int:
        return self.MAX_FILE_SIZE_MB * 1024 * 1024

    @property
    def DB_URL(self) -> str:
        return f"sqlite+aiosqlite:///{self.DB_PATH}"

    @property
    def cors_origins_list(self) -> list[str]:
        """
        Парсит CORS_ORIGINS в список. Понимает:
          - пустую строку           → [localhost дефолты]
          - "http://a.com,http://b" → [a, b]
          - '["http://a","http://b"]' → [a, b]  (JSON)
        """
        v = self.CORS_ORIGINS.strip()
        if not v:
            return ["http://localhost:8000", "http://127.0.0.1:8000"]
        if v.startswith("["):
            import json
            try:
                return json.loads(v)
            except Exception:
                pass
        return [o.strip() for o in v.split(",") if o.strip()]

    # ── Validators ──────────────────────────────────────────────────────────────

    @field_validator("STORAGE_PATH", "DB_PATH", "LOG_PATH", mode="after")
    @classmethod
    def _make_absolute(cls, v: Path) -> Path:
        return v if v.is_absolute() else Path.cwd() / v

    # ── Dirs ────────────────────────────────────────────────────────────────────

    def ensure_dirs(self) -> None:
        self.STORAGE_PATH.mkdir(parents=True, exist_ok=True)
        self.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.LOG_PATH.parent.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()  # type: ignore[call-arg]
    s.ensure_dirs()
    return s


# Этот класс уже определён выше — добавляем поле через monkey-patch не нужно.
# KAGGLE_ACCOUNTS добавляется как Field ниже через наследование не нужно —
# просто используем уже существующий Settings и добавим поле напрямую.
