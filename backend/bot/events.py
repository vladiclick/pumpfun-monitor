"""Tipurile de evenimente pe care botii le trimit catre backend."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Literal

EventType = Literal[
    "bot_started",     # botul a pornit
    "bot_stopped",     # botul s-a oprit (curat sau cu eroare)
    "ws_connected",    # conexiune WebSocket stabilita
    "ws_disconnected", # conexiune pierduta, urmeaza reconectare
    "token_detected",  # am vazut "Instruction: Create" in loguri
    "token_resolved",  # am extras mint + nume/simbol din tranzactie
    "verdict",         # pipeline-ul anti-rug s-a incheiat
    "hot",             # tokenul a luat foc in timpul supravegherii
    "error",           # eroare recuperabila
    "stats",           # heartbeat cu contoare
]

Verdict = Literal["PASS", "FAIL", "PENDING", "ERROR"]


@dataclass
class Event:
    """Un eveniment trimis catre FastAPI. `ts` e in ms epoch (usor de sortat in JS)."""

    bot_id: str
    type: EventType
    ts: int = field(default_factory=lambda: int(time.time() * 1000))
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    # Campuri specifice tokenului (optionale in functie de tipul evenimentului)
    signature: str | None = None
    mint: str | None = None
    name: str | None = None
    symbol: str | None = None
    uri: str | None = None
    creator: str | None = None

    verdict: Verdict | None = None
    reason: str | None = None
    failed_stage: str | None = None
    # Latenta reala: de la detectia in loguri pana la verdict, in milisecunde.
    latency_ms: int | None = None
    # Detalii per etapa (authority, lichiditate, rugcheck)
    checks: dict[str, Any] = field(default_factory=dict)

    message: str | None = None
    level: str = "info"
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        # Eliminam None-urile ca payload-ul sa fie mic (RPC public + WS pe telefon).
        return {k: v for k, v in asdict(self).items() if v is not None and v != {}}
