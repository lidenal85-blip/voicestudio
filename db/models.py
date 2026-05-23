"""
Модуль 2: db/models.py
SQLAlchemy ORM-модели для AI Voice Studio MVP.
Патч 1.1: добавлены Transcription, LyricLine.
           AudioAsset расширен полями sample_rate, channels, checksum.
           Project расширен полем updated_at.
           Job расширен полями started_at, completed_at.
           pipeline_state расширен: slicing добавлен между transcribing и ready.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _uuid4() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


# ── Project ────────────────────────────────────────────────────────────────────

class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid4)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="created"
    )  # created | processing | ready | failed
    pipeline_state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="uploaded"
    )  # uploaded | separating | transcribing | slicing | ready
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    assets: Mapped[list[AudioAsset]] = relationship(
        "AudioAsset", back_populates="project", cascade="all, delete-orphan"
    )
    jobs: Mapped[list[Job]] = relationship(
        "Job", back_populates="project", cascade="all, delete-orphan"
    )
    transcriptions: Mapped[list[Transcription]] = relationship(
        "Transcription", back_populates="project", cascade="all, delete-orphan"
    )


# ── AudioAsset ─────────────────────────────────────────────────────────────────

class AudioAsset(Base):
    __tablename__ = "audio_assets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid4)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # original | vocal | instrumental | phrase
    file_path: Mapped[str] = mapped_column(String(512), nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    format: Mapped[str | None] = mapped_column(String(16), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sample_rate: Mapped[int | None] = mapped_column(Integer, nullable=True)   # Hz
    channels: Mapped[int | None] = mapped_column(Integer, nullable=True)      # 1|2
    checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)   # sha256

    project: Mapped[Project] = relationship("Project", back_populates="assets")


# ── Job ────────────────────────────────────────────────────────────────────────

class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid4)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # separation | transcription | slicing
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending"
    )  # pending | processing | done | failed | cancelled
    input_params: Mapped[str | None] = mapped_column(Text, nullable=True)    # JSON
    output_result: Mapped[str | None] = mapped_column(Text, nullable=True)   # JSON
    error_msg: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    project: Mapped[Project] = relationship("Project", back_populates="jobs")


# ── Transcription ──────────────────────────────────────────────────────────────

class Transcription(Base):
    """
    Результат Whisper-транскрипции.
    Версионируется: при перезапуске создаётся новая запись (version++),
    старые не удаляются — пользователь может откатиться.
    """
    __tablename__ = "transcriptions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid4)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    language: Mapped[str | None] = mapped_column(String(8), nullable=True)   # "ru","en"
    confidence: Mapped[float | None] = mapped_column(nullable=True)          # 0.0-1.0
    lrc_content: Mapped[str | None] = mapped_column(Text, nullable=True)     # .lrc текст
    segments_json: Mapped[str | None] = mapped_column(Text, nullable=True)   # JSON Whisper-сегменты
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    project: Mapped[Project] = relationship("Project", back_populates="transcriptions")
    lines: Mapped[list[LyricLine]] = relationship(
        "LyricLine", back_populates="transcription", cascade="all, delete-orphan",
        order_by="LyricLine.index",
    )


# ── LyricLine ──────────────────────────────────────────────────────────────────

class LyricLine(Base):
    """
    Одна строка субтитров/lyrics.
    is_edited=True → пользователь правил вручную (защита от перезатирания).
    version → оптимистичная блокировка для Delta Update (аудитор п. 2.1).
    phrase_asset_id → ссылка на AudioAsset(type=phrase) после нарезки.
    """
    __tablename__ = "lyric_lines"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid4)
    transcription_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("transcriptions.id", ondelete="CASCADE"), nullable=False
    )
    index: Mapped[int] = mapped_column(Integer, nullable=False)   # порядковый номер строки
    text: Mapped[str] = mapped_column(Text, nullable=False)
    start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    is_edited: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    phrase_asset_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("audio_assets.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    transcription: Mapped[Transcription] = relationship(
        "Transcription", back_populates="lines"
    )
    phrase_asset: Mapped[AudioAsset | None] = relationship(
        "AudioAsset", foreign_keys=[phrase_asset_id]
    )


# ── Recording ──────────────────────────────────────────────────────────────────

class Recording(Base):
    """
    Запись пользователя поверх инструментала.
    timing_offset_ms — измеренная latency (аудитор п.2.1.3).
    Positive = задержать mic в миксе (mic был ранним из-за latency).
    """
    __tablename__ = "recordings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid4)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    mic_audio_path: Mapped[str] = mapped_column(String(512), nullable=False)
    mixed_audio_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    timing_offset_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    size_bytes_mic: Mapped[int | None] = mapped_column(Integer, nullable=True)
    size_bytes_mixed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending"
    )  # pending | processing | ready | failed
    error_msg: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    project: Mapped[Project] = relationship("Project")


# ── PipelineEvent (append-only audit log) ─────────────────────────────────────

class PipelineEvent(Base):
    """
    Лог событий Pipeline — append-only (Event Sourcing lite).
    Пишется при каждом переходе статуса, никогда не удаляется.
    """
    __tablename__ = "pipeline_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid4)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(
        String(64), nullable=False
    )  # uploaded | separation_started | separation_done | transcription_done
       # slicing_done | reprocess_requested | failed | ready
    data_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON метаданные
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
