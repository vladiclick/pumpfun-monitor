"""Hub de broadcast pentru clientii WebSocket ai dashboardului."""
from __future__ import annotations

import asyncio
import contextlib
import json
from collections import deque
from typing import Any

from fastapi import WebSocket
from starlette.websockets import WebSocketState

from bot.logging_setup import get_logger

log = get_logger("app.hub")


class Client:
    """Un dashboard conectat. Are propria coada, ca un client lent sa nu-i franeze pe ceilalti."""

    def __init__(self, ws: WebSocket, queue_size: int = 500) -> None:
        self.ws = ws
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=queue_size)
        self.dropped = 0

    def offer(self, message: str) -> None:
        """Pune mesajul in coada; daca e plina, arunca cel mai vechi."""
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
                self.dropped += 1
            with contextlib.suppress(asyncio.QueueFull):
                self.queue.put_nowait(message)


class Hub:
    def __init__(self, replay_size: int = 200) -> None:
        self._clients: set[Client] = set()
        self._lock = asyncio.Lock()
        # Buffer circular trimis la conectare, ca dashboardul sa nu porneasca gol.
        self._replay: deque[dict[str, Any]] = deque(maxlen=replay_size)

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def register(self, ws: WebSocket) -> Client:
        client = Client(ws)
        async with self._lock:
            self._clients.add(client)
        log.info("Dashboard conectat (%d activi)", len(self._clients))
        return client

    async def unregister(self, client: Client) -> None:
        async with self._lock:
            self._clients.discard(client)
        log.info("Dashboard deconectat (%d activi)", len(self._clients))

    def replay_buffer(self) -> list[dict[str, Any]]:
        return list(self._replay)

    async def broadcast(self, events: list[dict[str, Any]]) -> None:
        """Trimite un lot de evenimente tuturor clientilor."""
        if not events:
            return
        self._replay.extend(events)
        if not self._clients:
            return

        message = json.dumps({"type": "events", "events": events}, ensure_ascii=False)
        async with self._lock:
            targets = list(self._clients)
        for client in targets:
            client.offer(message)

    async def send_json(self, client: Client, payload: dict[str, Any]) -> None:
        client.offer(json.dumps(payload, ensure_ascii=False))

    async def broadcast_json(self, payload: dict[str, Any]) -> None:
        """Trimite un mesaj de control (nu eveniment) tuturor clientilor."""
        if not self._clients:
            return
        message = json.dumps(payload, ensure_ascii=False)
        async with self._lock:
            targets = list(self._clients)
        for client in targets:
            client.offer(message)

    async def pump(self, client: Client) -> None:
        """Bucla care scoate din coada si scrie efectiv pe socket."""
        try:
            while True:
                message = await client.queue.get()
                if client.ws.client_state != WebSocketState.CONNECTED:
                    return
                await client.ws.send_text(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # socket inchis brusc, retea picata etc.
            log.debug("Scriere WS esuata: %s", exc)
