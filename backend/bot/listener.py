"""Ascultator WebSocket pe logurile programului pump.fun.

Foloseste `logsSubscribe` cu filtru `mentions` pe programul pump.fun si emite
semnaturile tranzactiilor care contin "Instruction: Create".
Reconectarea e automata, cu backoff exponential + jitter.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import random
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable

import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException

from .config import BotConfig
from .logging_setup import get_logger
from .settings import PUMPFUN_PROGRAM_ID, mask_endpoint
from .trades import extrage_tranzactii

log = get_logger("bot.listener")

# Numele exacte ale instructiunilor de creare din programul pump.fun.
# Comparam EXACT, nu cu `in`: altfel prindem si "CreateFeeSharingConfig",
# "CreateTokenAccountWithSeed" etc. si irosim apeluri getTransaction.
CREATE_INSTRUCTIONS = frozenset({"Create", "CreateV2"})

_INSTRUCTION_LOG_PREFIX = "Program log: Instruction: "
# Superset strict al marker-elor acceptate: orice log valid de creare il contine.
_PREFILTER = "Instruction: Create"
_PREFILTER_BYTES = _PREFILTER.encode()
_PREFILTER_DATA = "Program data:"
_PREFILTER_DATA_BYTES = _PREFILTER_DATA.encode()
_INVOKE_RE = re.compile(r"^Program (\S+) invoke \[\d+\]$")
_END_RE = re.compile(r"^Program (\S+) (success|failed.*)$")


def logs_contain_pumpfun_create(logs: list) -> bool:
    """True doar daca instructiunea de creare apartine programului pump.fun.

    Logurile Solana sunt o urma a stivei de invocari:
        Program <ID> invoke [1]
        Program log: Instruction: CreateV2
        Program <ALT_ID> invoke [2]
        ...
        Program <ID> success
    Tinem evidenta programului aflat pe varful stivei si acceptam marker-ul
    doar cand acesta e pump.fun. Fara asta, orice tranzactie care mentioneaza
    pump.fun si creeaza un cont ATA ar declansa un getTransaction inutil.
    """
    stack: list[str] = []
    for line in logs:
        if not isinstance(line, str):
            continue

        invoke = _INVOKE_RE.match(line)
        if invoke:
            stack.append(invoke.group(1))
            continue

        end = _END_RE.match(line)
        if end:
            if stack and stack[-1] == end.group(1):
                stack.pop()
            elif stack:
                # Log neasteptat (trunchiat de nod): resincronizam prudent.
                stack.pop()
            continue

        if line.startswith(_INSTRUCTION_LOG_PREFIX):
            name = line[len(_INSTRUCTION_LOG_PREFIX):].strip()
            if name in CREATE_INSTRUCTIONS and stack and stack[-1] == PUMPFUN_PROGRAM_ID:
                return True
    return False


@dataclass
class Detection:
    """O lansare detectata in loguri, cu tot ce stim in acel moment.

    Pastram si logurile: ele contin evenimentul `CreateEvent`, deci in cazul
    fericit nu mai e nevoie de niciun apel RPC ca sa aflam mint-ul si numele.
    """

    signature: str
    logs: list[str] = field(default_factory=list)
    detected_at: float = 0.0


class SeenCache:
    """Set LRU cu dimensiune fixa, ca sa nu procesam de doua ori aceeasi semnatura."""

    def __init__(self, maxsize: int = 5000) -> None:
        self.maxsize = maxsize
        self._data: OrderedDict[str, None] = OrderedDict()

    def add_if_new(self, key: str) -> bool:
        """True daca cheia e noua (si a fost adaugata)."""
        if key in self._data:
            self._data.move_to_end(key)
            return False
        self._data[key] = None
        if len(self._data) > self.maxsize:
            self._data.popitem(last=False)
        return True

    def __len__(self) -> int:
        return len(self._data)


class PumpFunListener:
    """Genereaza semnaturi de tranzactii `create` pana la oprire."""

    def __init__(
        self,
        cfg: BotConfig,
        ws_url: str,
        *,
        on_connect: Callable[[], None] | None = None,
        on_disconnect: Callable[[str], None] | None = None,
        on_trades: Callable[[list], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.ws_url = ws_url
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect
        # Apelat pentru fiecare lot de tranzactii citite din loguri. Aici sta
        # avantajul: pretul si cumparaturile vin instant, nu de la un API.
        self.on_trades = on_trades
        self.tranzactii_vazute = 0
        self.seen = SeenCache()
        self._stop = asyncio.Event()
        self.connected = False
        self.reconnects = 0
        self.logs_received = 0

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------
    async def detections(self) -> AsyncIterator[Detection]:
        """Bucla principala: conecteaza, asculta, reconecteaza la eroare."""
        backoff = self.cfg.ws_backoff_initial

        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    self.ws_url,
                    ping_interval=self.cfg.ws_ping_interval,
                    ping_timeout=self.cfg.ws_ping_timeout,
                    close_timeout=5,
                    open_timeout=10,
                    max_queue=512,
                    # Logurile Solana pot fi mari; 8 MB e generos dar sigur.
                    max_size=8 * 1024 * 1024,
                ) as ws:
                    await self._subscribe(ws)
                    self.connected = True
                    backoff = self.cfg.ws_backoff_initial  # reset dupa succes
                    if self.on_connect:
                        self.on_connect()
                    log.info("[%s] WebSocket conectat la %s", self.cfg.bot_id, mask_endpoint(self.ws_url))

                    async for detection in self._read_loop(ws):
                        yield detection

            except asyncio.CancelledError:
                raise
            except InvalidStatus as exc:
                # 401/403/429 la handshake: cheie gresita sau rate limit pe provider.
                reason = f"handshake respins: HTTP {getattr(exc.response, 'status_code', '?')}"
                self._handle_drop(reason)
            except (ConnectionClosed, WebSocketException, OSError) as exc:
                self._handle_drop(f"{type(exc).__name__}: {exc}")
            except Exception as exc:  # orice altceva - nu oprim botul
                self._handle_drop(f"eroare neasteptata: {type(exc).__name__}: {exc}")

            if self._stop.is_set():
                break

            # Backoff exponential cu jitter, ca sa nu lovim nodul in acelasi moment
            # cu celelalte instante de bot.
            sleep_for = min(backoff, self.cfg.ws_backoff_max) * (0.75 + random.random() * 0.5)
            log.info("[%s] Reconectare in %.1fs...", self.cfg.bot_id, sleep_for)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=sleep_for)
                break  # s-a cerut oprirea in timpul asteptarii
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, self.cfg.ws_backoff_max)
            self.reconnects += 1

    # ------------------------------------------------------------------
    def _handle_drop(self, reason: str) -> None:
        was_connected = self.connected
        self.connected = False
        if was_connected:
            log.warning("[%s] WebSocket pierdut: %s", self.cfg.bot_id, reason)
        else:
            log.warning("[%s] Conectare esuata: %s", self.cfg.bot_id, reason)
        if self.on_disconnect:
            self.on_disconnect(reason)

    async def _subscribe(self, ws) -> None:
        """Trimite logsSubscribe si asteapta confirmarea abonarii."""
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "logsSubscribe",
            "params": [
                {"mentions": [PUMPFUN_PROGRAM_ID]},
                {"commitment": "processed"},  # cel mai rapid; validam ulterior prin getTransaction
            ],
        }
        await ws.send(json.dumps(request))

        # Confirmarea vine imediat; daca nu, endpoint-ul nu suporta logsSubscribe.
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=15.0)
        except asyncio.TimeoutError as exc:
            raise WebSocketException("nodul nu a confirmat logsSubscribe in 15s") from exc

        try:
            reply = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise WebSocketException(f"raspuns invalid la subscribe: {raw!r:.120}") from exc

        if isinstance(reply, dict) and reply.get("error"):
            err = reply["error"]
            msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            raise WebSocketException(f"logsSubscribe respins de nod: {msg}")

        sub_id = reply.get("result") if isinstance(reply, dict) else None
        log.debug("[%s] Abonat cu subscription id=%s", self.cfg.bot_id, sub_id)

    async def _recv_or_stop(self, ws, timeout: float):
        """Asteapta un mesaj SAU cererea de oprire, ce vine prima.

        Fara asta, `stop()` era observat abia la urmatoarea iteratie a buclei:
        pe o conexiune linistita puteau trece pana la 60s, iar managerul apuca
        sa trimita SIGKILL inainte ca botul sa apuce sa se inchida curat.
        """
        recv_task = asyncio.create_task(ws.recv())
        stop_task = asyncio.create_task(self._stop.wait())
        try:
            done, _ = await asyncio.wait(
                {recv_task, stop_task},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stop_task in done:
                return None
            if recv_task in done:
                return recv_task.result()
            raise asyncio.TimeoutError
        finally:
            for task in (recv_task, stop_task):
                if not task.done():
                    task.cancel()
            # Consumam eventuala exceptie a task-ului anulat, ca sa nu apara
            # "Task exception was never retrieved".
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await recv_task

    async def _read_loop(self, ws) -> AsyncIterator[Detection]:
        """Citeste notificarile si filtreaza cele care contin instructiunea Create."""
        while not self._stop.is_set():
            try:
                raw = await self._recv_or_stop(ws, timeout=self.cfg.ws_ping_interval * 3)
            except asyncio.TimeoutError:
                # Fara mesaje mult timp: fortam un ping ca sa detectam conexiuni moarte.
                try:
                    pong_waiter = await ws.ping()
                    await asyncio.wait_for(pong_waiter, timeout=self.cfg.ws_ping_timeout)
                except (asyncio.TimeoutError, WebSocketException, OSError) as exc:
                    raise ConnectionClosed(None, None) from exc
                continue

            if raw is None:
                return  # s-a cerut oprirea

            detection = self._extract(raw)
            if detection is not None:
                yield detection

    def _extract(self, raw: str | bytes) -> Detection | None:
        """Parsare defensiva a notificarii logsNotification."""
        self.logs_received += 1

        # Prefiltru ieftin inainte de json.loads. Programul pump.fun genereaza
        # ~1400 mesaje/s, iar sub 0.1% sunt creari. Un mesaj care nu contine
        # deloc textul de mai jos NU poate contine marker-ul cautat, deci il
        # putem arunca fara sa-l parsam. Masurat: 10.8us -> 0.5us per mesaj.
        # Prefiltru: ne intereseaza fie o creare, fie o tranzactie. Ambele au
        # "Program data:" in loguri; restul mesajelor (invocari, compute units)
        # se arunca fara sa fie parsate.
        if isinstance(raw, bytes):
            if _PREFILTER_BYTES not in raw and _PREFILTER_DATA_BYTES not in raw:
                return None
        elif _PREFILTER not in raw and _PREFILTER_DATA not in raw:
            return None

        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            log.debug("[%s] Mesaj WS neparsabil, il ignor", self.cfg.bot_id)
            return None

        if not isinstance(msg, dict) or msg.get("method") != "logsNotification":
            return None

        value = (((msg.get("params") or {}).get("result")) or {}).get("value")
        if not isinstance(value, dict):
            return None

        # Tranzactiile esuate apar tot in loguri; le sarim din start.
        if value.get("err"):
            return None

        logs = value.get("logs")
        if not isinstance(logs, list):
            return None

        # Tranzactiile se predau imediat, indiferent daca mesajul e o creare.
        if self.on_trades is not None:
            tranzactii = extrage_tranzactii(logs)
            if tranzactii:
                self.tranzactii_vazute += len(tranzactii)
                try:
                    self.on_trades(tranzactii)
                except Exception:
                    log.exception("Handler-ul de tranzactii a aruncat")

        if not logs_contain_pumpfun_create(logs):
            return None

        signature = value.get("signature")
        if not isinstance(signature, str) or len(signature) < 64:
            return None

        if not self.seen.add_if_new(signature):
            return None
        return Detection(signature=signature, logs=logs, detected_at=time.monotonic())


