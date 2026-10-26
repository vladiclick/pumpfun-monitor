"""Trimiterea evenimentelor catre backend-ul FastAPI.

Nu vrem ca botul sa se blocheze sau sa moara daca backend-ul e jos: evenimentele
intra intr-o coada in memorie si sunt trimise de un worker separat, cu retry.
Daca backend-ul lipseste prea mult, cele mai vechi evenimente sunt aruncate
(pastram intotdeauna ultimele N) - un bot de monitorizare nu are voie sa umple RAM-ul.
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import httpx

from .events import Event
from .logging_setup import get_logger

log = get_logger("bot.emitter")


class EventEmitter:
    def __init__(
        self,
        backend_url: str,
        ingest_token: str,
        *,
        queue_size: int = 2000,
        timeout: float = 5.0,
        batch_size: int = 25,
    ) -> None:
        self.url = f"{backend_url.rstrip('/')}/api/ingest"
        self.token = ingest_token
        self.batch_size = batch_size
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=queue_size)
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=3.0),
            headers={"x-ingest-token": ingest_token, "content-type": "application/json"},
        )
        self._task: asyncio.Task | None = None
        self._stopped = asyncio.Event()
        self._dropped = 0
        self._sent = 0
        self._backend_down_logged = False

    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._worker(), name="event-emitter")

    async def stop(self, drain_timeout: float = 3.0) -> None:
        """Incearca sa trimita ce a ramas, apoi inchide clientul."""
        self._stopped.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(self._task, timeout=drain_timeout)
            if not self._task.done():
                self._task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._task
        await self._client.aclose()
        if self._dropped:
            log.warning("Emitter oprit; %d evenimente pierdute (backend indisponibil)", self._dropped)

    # ------------------------------------------------------------------
    def emit(self, event: Event) -> None:
        """Non-blocant. Daca coada e plina, arunca cel mai vechi eveniment."""
        payload = event.to_dict()
        try:
            self._queue.put_nowait(payload)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
                self._dropped += 1
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(payload)

    # ------------------------------------------------------------------
    async def _worker(self) -> None:
        """Bucla de trimitere. Nu are voie sa moara: daca task-ul asta cade,
        botul continua sa ruleze dar nu mai raporteaza nimic, tacut."""
        backoff = 1.0
        while not (self._stopped.is_set() and self._queue.empty()):
            try:
                backoff = await self._send_once(backoff)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Orice eroare neprevazuta (bug propriu, biblioteca) - logam si
                # continuam. Alternativa ar fi oprirea definitiva a raportarii.
                log.exception("Eroare neasteptata in emitter: %s", exc)
                await asyncio.sleep(min(backoff, 5.0))
                backoff = min(backoff * 2, 30.0)

    async def _send_once(self, backoff: float) -> float:
        """Un ciclu: ia un lot, il trimite, intoarce noul backoff."""
        batch = await self._collect_batch()
        if not batch:
            return backoff

        try:
            resp = await self._client.post(self.url, json={"events": batch})
        except asyncio.CancelledError:
            # Oprire in curs: punem lotul inapoi ca sa fie prins de drenaj.
            await self._requeue(batch)
            raise
        except Exception as exc:
            # Larg intentionat. httpx.HTTPError acopera si DecodingError /
            # TooManyRedirects (care NU sunt subclase de TransportError), dar
            # orice alta eroare neprevazuta trebuie sa duca tot la retrimitere,
            # nu la pierderea tacuta a evenimentelor.
            if isinstance(exc, httpx.HTTPError):
                if not self._backend_down_logged:
                    log.warning("Backend inaccesibil (%s). Retrimit in fundal.", exc)
                    self._backend_down_logged = True
            else:
                log.exception("Eroare neasteptata la trimiterea lotului: %s", exc)
            await self._requeue(batch)
            await asyncio.sleep(backoff)
            return min(backoff * 2, 30.0)

        if resp.status_code == 401:
            # Token gresit: retrimiterea n-are rost, ar bucla la infinit.
            log.error("Ingest respins (401). Verifica INGEST_TOKEN in .env.")
            self._dropped += len(batch)
            await asyncio.sleep(5.0)
            return backoff

        if resp.status_code == 429 or resp.status_code >= 500:
            await self._requeue(batch)
            await asyncio.sleep(backoff)
            return min(backoff * 2, 30.0)

        if resp.status_code >= 300:
            # 3xx nu inseamna succes: clientul nu urmareste redirectari, deci
            # lotul nu a ajuns nicaieri. Inainte cadea pe ramura de succes.
            log.error("Ingest a refuzat lotul (HTTP %d): %s", resp.status_code, resp.text[:200])
            self._dropped += len(batch)
            return backoff

        self._sent += len(batch)
        if self._backend_down_logged:
            log.info("Conexiunea cu backend-ul a revenit.")
            self._backend_down_logged = False
        return 1.0

    async def _collect_batch(self) -> list[dict[str, Any]]:
        """Ia primul eveniment (asteptand), apoi goleste coada pana la batch_size."""
        batch: list[dict[str, Any]] = []
        try:
            first = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            batch.append(first)
        except asyncio.TimeoutError:
            return batch

        while len(batch) < self.batch_size:
            try:
                batch.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return batch

    async def _requeue(self, batch: list[dict[str, Any]]) -> None:
        """Pune lotul inapoi in coada pentru o noua incercare.

        Ordinea nu e garantata dupa un requeue (asyncio.Queue e FIFO simplu),
        dar fiecare eveniment are `ts`, iar dashboardul sorteaza dupa el.
        """
        for payload in batch:
            try:
                self._queue.put_nowait(payload)
            except asyncio.QueueFull:
                self._dropped += 1

    @property
    def stats(self) -> dict[str, int]:
        return {"sent": self._sent, "dropped": self._dropped, "queued": self._queue.qsize()}
