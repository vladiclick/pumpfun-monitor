"""Client JSON-RPC Solana peste httpx, cu rate limiting, retry si erori tipizate.

Nu folosim `solana.rpc.async_api.AsyncClient` direct pentru ca avem nevoie de
control fin pe retry / rate limit (RPC-ul public da 429 foarte usor), dar
raspunsurile sunt aceleasi structuri JSON-RPC.
"""
from __future__ import annotations

import asyncio
import random
import time
from typing import Any

import httpx

from .logging_setup import get_logger

log = get_logger("bot.rpc")


class RpcError(RuntimeError):
    """Eroare returnata explicit de nodul RPC (campul `error` din raspuns)."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"RPC {code}: {message}")
        self.code = code
        self.message = message


class RpcUnavailable(RuntimeError):
    """Nodul nu a raspuns dupa toate retry-urile (retea, timeout, 5xx, 429)."""


class RateLimiter:
    """Rate limiter simplu: garanteaza o pauza minima intre apeluri."""

    def __init__(self, min_interval: float) -> None:
        self.min_interval = max(0.0, min_interval)
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def acquire(self) -> None:
        if self.min_interval <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            wait = self._last + self.min_interval - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()


class SolanaRpc:
    """Wrapper peste endpoint-ul HTTP de RPC."""

    def __init__(
        self,
        endpoint: str,
        *,
        timeout: float = 10.0,
        min_interval: float = 0.35,
        max_retries: int = 3,
    ) -> None:
        self.endpoint = endpoint
        self.max_retries = max_retries
        self._limiter = RateLimiter(min_interval)
        self._id = 0
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=min(timeout, 5.0)),
            headers={"content-type": "application/json"},
            # Un pool mic e suficient si evita sa lovim rate-limit-ul in paralel.
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    async def call(
        self,
        method: str,
        params: list[Any] | None = None,
        *,
        deadline: float | None = None,
    ) -> Any:
        """Apel JSON-RPC cu retry exponential + jitter pe erori tranzitorii.

        `deadline` (timestamp `time.monotonic()`) opreste retry-urile cand
        fereastra utila a expirat. Fara el, un singur apel putea consuma pana la
        ~100s (4 incercari x timeout + backoff) si tinea ocupat un slot din
        semaforul de concurenta mult peste durata in care verdictul mai conta.
        """
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
            "params": params or [],
        }

        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            if deadline is not None and time.monotonic() >= deadline and attempt > 0:
                raise RpcUnavailable(
                    f"{method}: fereastra utila a expirat dupa {attempt} incercari ({last_err})"
                )

            await self._limiter.acquire()
            try:
                resp = await self._client.post(self.endpoint, json=payload)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_err = exc
                log.debug("%s: eroare retea (incercarea %d): %s", method, attempt + 1, exc)
            else:
                # 429 / 5xx = tranzitorii, merita retry. 4xx = eroare de client.
                if resp.status_code == 429 or resp.status_code >= 500:
                    last_err = RpcUnavailable(f"HTTP {resp.status_code}")
                    retry_after = _parse_retry_after(resp.headers.get("retry-after"))
                    if retry_after:
                        await asyncio.sleep(min(retry_after, 10.0))
                    log.debug("%s: HTTP %d (incercarea %d)", method, resp.status_code, attempt + 1)
                elif resp.status_code >= 400:
                    raise RpcUnavailable(f"HTTP {resp.status_code}: {resp.text[:200]}")
                else:
                    try:
                        body = resp.json()
                    except ValueError as exc:
                        raise RpcUnavailable(f"raspuns non-JSON de la RPC: {resp.text[:200]}") from exc

                    if isinstance(body, dict) and body.get("error"):
                        err = body["error"]
                        code = err.get("code", -1) if isinstance(err, dict) else -1
                        msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                        # -32005 = node behind / rate limited => retry
                        if code in (-32005, -32603):
                            last_err = RpcError(code, msg)
                            log.debug("%s: eroare RPC tranzitorie %s", method, msg)
                        else:
                            raise RpcError(code, msg)
                    else:
                        return body.get("result") if isinstance(body, dict) else body

            if attempt < self.max_retries:
                backoff = min(2**attempt * 0.4, 5.0) + random.uniform(0, 0.25)
                if deadline is not None:
                    # Nu dormim dincolo de deadline degeaba.
                    backoff = min(backoff, max(0.0, deadline - time.monotonic()))
                await asyncio.sleep(backoff)

        raise RpcUnavailable(f"{method} a esuat dupa {self.max_retries + 1} incercari: {last_err}")

    # ------------------------------------------------------------------
    async def get_transaction(self, signature: str) -> dict[str, Any] | None:
        """Tranzactia in format jsonParsed. None daca nu e (inca) disponibila."""
        result = await self.call(
            "getTransaction",
            [
                signature,
                {
                    "encoding": "jsonParsed",
                    "commitment": "confirmed",
                    "maxSupportedTransactionVersion": 0,
                },
            ],
        )
        return result if isinstance(result, dict) else None

    async def get_account_info_parsed(
        self, address: str, commitment: str = "processed", *, deadline: float | None = None
    ) -> dict[str, Any] | None:
        """getAccountInfo cu encoding jsonParsed. None daca contul nu exista.

        Implicit folosim `processed`: detectam lansarea tot la `processed`, iar
        pe `confirmed` contul de mint inca nu exista de multe ori (cursa de
        cateva sute de ms). Pentru monitorizare e acceptabil - starea poate fi
        teoretic anulata la un fork, dar autoritatile unui mint nu se schimba.
        """
        result = await self.call(
            "getAccountInfo",
            [address, {"encoding": "jsonParsed", "commitment": commitment}],
            deadline=deadline,
        )
        if not isinstance(result, dict):
            return None
        return result.get("value")

    async def get_health(self) -> str:
        """Ping rapid pe nod. Returneaza 'ok' sau mesajul de eroare."""
        try:
            result = await self.call("getHealth")
            return str(result)
        except (RpcError, RpcUnavailable) as exc:
            return f"unhealthy: {exc}"


def _parse_retry_after(value: str | None) -> float | None:
    """Header-ul Retry-After poate lipsi sau fi in secunde."""
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None
