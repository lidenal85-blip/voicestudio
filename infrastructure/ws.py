"""
infrastructure/ws.py
WebSocket-менеджер для трансляции прогресса обработки.
In-memory (без Redis) — достаточно для MVP на одном процессе Termux.

Клиент подключается: GET /ws/projects/{project_id}
Сервер шлёт JSON-события:
  { "event": "progress", "project_id": "...", "stage": "slicing", "percent": 42 }
  { "event": "done",     "project_id": "...", "status": "ready" }
  { "event": "error",    "project_id": "...", "message": "..." }
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

router = APIRouter(tags=["websocket"])


class ConnectionManager:
    """
    Управляет WebSocket-соединениями, сгруппированными по project_id.
    Потокобезопасен для одного event-loop (asyncio).
    """

    def __init__(self) -> None:
        # project_id → set of WebSocket
        self._connections: dict[str, set[WebSocket]] = defaultdict(set)

    async def connect(self, project_id: str, ws: WebSocket) -> None:
        await ws.accept()
        self._connections[project_id].add(ws)
        logger.debug("WS connect: project=%s total=%d",
                     project_id, len(self._connections[project_id]))

    def disconnect(self, project_id: str, ws: WebSocket) -> None:
        self._connections[project_id].discard(ws)
        if not self._connections[project_id]:
            del self._connections[project_id]
        logger.debug("WS disconnect: project=%s", project_id)

    async def broadcast(self, project_id: str, payload: dict[str, Any]) -> None:
        """Шлёт payload всем подключённым клиентам проекта."""
        payload["project_id"] = project_id
        msg = json.dumps(payload, ensure_ascii=False)

        dead: list[WebSocket] = []
        for ws in list(self._connections.get(project_id, [])):
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)

        for ws in dead:
            self.disconnect(project_id, ws)

    async def send_progress(
        self,
        project_id: str,
        stage: str,
        percent: int,
        message: str = "",
    ) -> None:
        await self.broadcast(project_id, {
            "event":   "progress",
            "stage":   stage,
            "percent": percent,
            "message": message,
        })

    async def send_done(self, project_id: str, status: str = "ready") -> None:
        await self.broadcast(project_id, {
            "event":  "done",
            "status": status,
        })

    async def send_error(self, project_id: str, message: str) -> None:
        await self.broadcast(project_id, {
            "event":   "error",
            "message": message,
        })


# Синглтон — импортируй и используй везде
ws_manager = ConnectionManager()


# ── WebSocket endpoint ─────────────────────────────────────────────────────────

@router.websocket("/ws/projects/{project_id}")
async def ws_project_progress(
    websocket: WebSocket,
    project_id: str,
) -> None:
    """
    Клиент подписывается на прогресс обработки проекта.
    Соединение держится открытым до завершения обработки или дисконнекта.
    """
    await ws_manager.connect(project_id, websocket)
    try:
        # Держим соединение живым — ждём пинга или дисконнекта
        while True:
            data = await asyncio.wait_for(websocket.receive_text(), timeout=30)
            if data == "ping":
                await websocket.send_text('{"event":"pong"}')
    except (WebSocketDisconnect, asyncio.TimeoutError):
        pass
    except Exception as exc:
        logger.debug("WS error project=%s: %s", project_id, exc)
    finally:
        ws_manager.disconnect(project_id, websocket)
