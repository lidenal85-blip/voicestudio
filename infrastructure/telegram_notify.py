"""
infrastructure/telegram_notify.py
Telegram-уведомления о завершении обработки.
Требует: TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID в .env
"""

from __future__ import annotations

import logging
import httpx

logger = logging.getLogger(__name__)


async def send_telegram(token: str, chat_id: str, text: str) -> bool:
    """Отправляет сообщение. Возвращает True при успехе."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, json={
                "chat_id":    chat_id,
                "text":       text,
                "parse_mode": "HTML",
            })
            return r.status_code == 200
    except Exception as e:
        logger.warning("telegram notify failed: %s", e)
        return False


async def notify_ready(settings, project_title: str, stats: dict) -> None:
    """Уведомление о готовности проекта."""
    if not settings.telegram_enabled:
        return

    lines      = stats.get("lines", 0)
    confidence = stats.get("confidence", 0)
    conf_str   = f"{confidence*100:.0f}%" if confidence else "—"

    text = (
        f"🎵 <b>{project_title}</b> готов!\n"
        f"├ Строк LRC: {lines}\n"
        f"├ Точность Whisper: {conf_str}\n"
        f"└ <a href='{settings.base_url}'>Открыть студию</a>"
    )
    await send_telegram(settings.TELEGRAM_BOT_TOKEN, settings.TELEGRAM_CHAT_ID, text)


async def notify_failed(settings, project_title: str, reason: str) -> None:
    """Уведомление об ошибке."""
    if not settings.telegram_enabled:
        return

    text = (
        f"❌ <b>{project_title}</b> — ошибка обработки\n"
        f"└ {reason[:200]}"
    )
    await send_telegram(settings.TELEGRAM_BOT_TOKEN, settings.TELEGRAM_CHAT_ID, text)


async def notify_worker_started(settings, source: str = "kaggle") -> None:
    """Уведомление о запуске воркера."""
    if not settings.telegram_enabled:
        return
    await send_telegram(
        settings.TELEGRAM_BOT_TOKEN, settings.TELEGRAM_CHAT_ID,
        f"⚡ GPU воркер запущен ({source})"
    )
