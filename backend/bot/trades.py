"""Citirea tranzactiilor pump.fun direct din fluxul WebSocket.

De ce exista: pana acum asteptam DexScreener, care indexeaza perechea abia la
~45 de secunde dupa lansare si isi actualizeaza ferestrele cu intarziere. Dar
programul pump.fun emite fiecare tranzactie ca eveniment Anchor `TradeEvent`
in aceleasi loguri pe care le primim deja. Decodandu-le, avem pretul, sensul
(cumparare/vanzare) si capitalizarea in timp real, fara nicio cerere in plus.
"""
from __future__ import annotations

import base64
import binascii
import struct
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque

from .logging_setup import get_logger
from .solana_util import anchor_discriminator, b58encode

log = get_logger("bot.trades")

TRADE_EVENT_DISCRIMINATOR = anchor_discriminator("event", "TradeEvent")

# Lungimea minima a evenimentului: 8 discriminator + mint(32) + sol(8) +
# tokens(8) + is_buy(1) + user(32) + timestamp(8) + rezerve virtuale(8+8).
_LUNGIME_MINIMA = 113

# pump.fun emite 1 miliard de token-uri pentru fiecare moneda.
OFERTA_TOTALA = 1_000_000_000


@dataclass
class Tranzactie:
    """O cumparare sau vanzare, decodata din loguri."""

    mint: str
    sol: float
    tokens: float
    cumparare: bool
    pret_sol: float          # pret in SOL per token, din rezervele virtuale
    la: float = field(default_factory=time.monotonic)


def decodeaza_trade(raw: bytes) -> Tranzactie | None:
    """Decodeaza un `TradeEvent`. None daca nu e unul sau e trunchiat."""
    if len(raw) < _LUNGIME_MINIMA or raw[:8] != TRADE_EVENT_DISCRIMINATOR:
        return None
    try:
        pozitie = 8
        mint = b58encode(raw[pozitie:pozitie + 32]); pozitie += 32
        sol, tokens = struct.unpack_from("<QQ", raw, pozitie); pozitie += 16
        cumparare = raw[pozitie] == 1; pozitie += 1
        pozitie += 32 + 8                      # user + timestamp, nu ne trebuie
        vsol, vtok = struct.unpack_from("<QQ", raw, pozitie)
    except (struct.error, IndexError, ValueError):
        return None

    if vtok <= 0:
        return None
    # Rezervele virtuale dau pretul exact la care s-a executat tranzactia.
    pret = (vsol / 1e9) / (vtok / 1e6)
    return Tranzactie(
        mint=mint, sol=sol / 1e9, tokens=tokens / 1e6,
        cumparare=cumparare, pret_sol=pret,
    )


def extrage_tranzactii(logs: Any) -> list[Tranzactie]:
    """Toate tranzactiile dintr-o notificare de loguri."""
    if not isinstance(logs, list):
        return []
    gasite: list[Tranzactie] = []
    for linie in logs:
        if not isinstance(linie, str) or not linie.startswith("Program data:"):
            continue
        try:
            raw = base64.b64decode(linie.split("Program data:", 1)[1].strip(), validate=True)
        except (ValueError, binascii.Error):
            continue
        t = decodeaza_trade(raw)
        if t is not None:
            gasite.append(t)
    return gasite


@dataclass
class Fereastra:
    """Tranzactiile recente ale unui token."""

    tranzactii: Deque[Tranzactie] = field(default_factory=deque)
    prima_vazuta: float = field(default_factory=time.monotonic)
    total_tranzactii: int = 0


class TradeTracker:
    """Tine tranzactiile recente pentru token-urile supravegheate.

    Doar pentru mint-urile inregistrate explicit - programul pump.fun genereaza
    peste 1000 de mesaje pe secunda si nu are rost sa retinem tot.
    """

    def __init__(self, fereastra_secunde: float = 60.0, pret_sol_usd: float = 180.0) -> None:
        self.fereastra = fereastra_secunde
        self.pret_sol_usd = pret_sol_usd
        self._date: dict[str, Fereastra] = {}
        self.tranzactii_vazute = 0
        self.tranzactii_retinute = 0

    def urmareste(self, mint: str) -> None:
        self._date.setdefault(mint, Fereastra())

    def uita(self, mint: str) -> None:
        self._date.pop(mint, None)

    @property
    def urmarite(self) -> int:
        return len(self._date)

    def adauga(self, t: Tranzactie) -> bool:
        """Retine tranzactia daca tokenul e supravegheat. True daca a retinut-o."""
        self.tranzactii_vazute += 1
        f = self._date.get(t.mint)
        if f is None:
            return False
        f.tranzactii.append(t)
        f.total_tranzactii += 1
        self.tranzactii_retinute += 1
        self._curata(f)
        return True

    def _curata(self, f: Fereastra) -> None:
        limita = time.monotonic() - self.fereastra
        while f.tranzactii and f.tranzactii[0].la < limita:
            f.tranzactii.popleft()

    def masoara(self, mint: str) -> dict[str, Any] | None:
        """Indicatorii tokenului pe fereastra curenta. None daca nu stim nimic."""
        f = self._date.get(mint)
        if f is None:
            return None
        self._curata(f)
        if not f.tranzactii:
            return None

        cumparari = sum(1 for t in f.tranzactii if t.cumparare)
        vanzari = len(f.tranzactii) - cumparari
        volum_sol = sum(t.sol for t in f.tranzactii)
        primul, ultimul = f.tranzactii[0], f.tranzactii[-1]
        variatie = (
            (ultimul.pret_sol - primul.pret_sol) / primul.pret_sol * 100
            if primul.pret_sol > 0 else 0.0
        )
        mcap = ultimul.pret_sol * OFERTA_TOTALA * self.pret_sol_usd

        return {
            "market_cap": mcap,
            "price_change_m5": variatie,          # pe fereastra noastra, nu 5 min
            "volume_m5": volum_sol * self.pret_sol_usd,
            "volume_h1": None,                    # nu avem istoric asa lung aici
            "buys_m5": cumparari,
            "sells_m5": vanzari,
            "price_usd": ultimul.pret_sol * self.pret_sol_usd,
            "sursa": "loguri live",
            "fereastra_s": round(self.fereastra),
            "tranzactii_totale": f.total_tranzactii,
            "varsta_s": round(time.monotonic() - f.prima_vazuta),
        }
