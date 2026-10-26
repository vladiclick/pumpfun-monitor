"""Scheme Pydantic pentru API (validare intrari + documentatie automata)."""
from __future__ import annotations

from typing import Any

import json

from pydantic import BaseModel, Field, field_validator, model_validator


# Un eveniment normal are ~1-2 KB. Peste asta e fie un bug in bot, fie abuz.
MAX_EVENT_BYTES = 64 * 1024


class IngestEvent(BaseModel):
    """Un eveniment primit de la un bot. Permitem campuri extra (forward-compatible)."""

    model_config = {"extra": "allow"}

    event_id: str = Field(min_length=8, max_length=64)
    bot_id: str = Field(min_length=1, max_length=64)
    type: str = Field(min_length=1, max_length=32)
    ts: int = Field(gt=0)

    @model_validator(mode="after")
    def _bound_total_size(self) -> "IngestEvent":
        """`extra: allow` lasa `data`/`checks` sa creasca oricat; le plafonam.

        Fara asta, un bot cu un bug (sau un client rau intentionat care are
        token-ul) putea trimite evenimente de zeci de MB, care ajungeau in
        SQLite si erau difuzate catre toate dashboard-urile.
        """
        try:
            size = len(json.dumps(self.model_dump(), default=str))
        except (TypeError, ValueError):
            raise ValueError("evenimentul nu poate fi serializat")
        if size > MAX_EVENT_BYTES:
            raise ValueError(
                f"eveniment prea mare ({size} bytes, maxim {MAX_EVENT_BYTES})"
            )
        return self

    @field_validator("ts")
    @classmethod
    def _sane_timestamp(cls, v: int) -> int:
        # Respingem timestamp-uri absurde (ex. secunde in loc de milisecunde).
        if v < 1_000_000_000_000 or v > 4_000_000_000_000:
            raise ValueError("ts trebuie sa fie epoch in milisecunde")
        return v


class IngestPayload(BaseModel):
    events: list[IngestEvent] = Field(min_length=1, max_length=200)


class BotConfigPayload(BaseModel):
    """Config trimis din dashboard la creare/editare de instanta."""

    model_config = {"extra": "allow"}

    bot_id: str = Field(min_length=2, max_length=32, pattern=r"^[a-z0-9][a-z0-9_-]{1,31}$")
    label: str = ""

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump()


class ActionResponse(BaseModel):
    ok: bool = True
    message: str = ""
    instance: dict[str, Any] | None = None
