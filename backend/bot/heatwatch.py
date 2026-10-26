"""Supraveghere comuna a token-urilor, cu cereri grupate catre DexScreener.

De ce exista: varianta initiala interoga fiecare token separat, din interiorul
pipeline-ului. La ~0.85 lansari/secunda si o fereastra de 20 de minute asta
insemna peste 100 de token-uri "in lucru" simultan, ~23 cereri/secunda catre
DexScreener (deci 429-uri) si detectii aruncate pentru ca nu mai erau sloturi.

Aici token-urile stau intr-un singur set, iar DexScreener accepta pana la 30 de
adrese intr-o singura cerere. 600 de token-uri supravegheate = 20 de cereri pe
ciclu, nu 600. Asta permite interogare la cateva secunde in loc de zeci.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from .config import BotConfig
from .logging_setup import get_logger

log = get_logger("bot.heatwatch")

# Limita reala a API-ului: 30 de adrese merg, 50 dau HTTP 400.
MAX_ADRESE_PE_CERERE = 30
DEXSCREENER_TOKENS = "https://api.dexscreener.com/latest/dex/tokens/{mints}"


@dataclass
class Urmarit:
    """Un token aflat sub supraveghere."""

    mint: str
    meta: dict[str, Any]
    adaugat_la: float = field(default_factory=time.monotonic)
    detectat_la: float = 0.0
    citiri: int = 0
    cap_maxim: float = 0.0


def masoara_pereche(pair: dict[str, Any]) -> dict[str, Any]:
    """Extrage indicatorii de piata dintr-o pereche DexScreener."""
    tx = (pair.get("txns") or {}).get("m5") or {}
    return {
        "market_cap": _numar(pair.get("marketCap") or pair.get("fdv")),
        "price_change_m5": _numar((pair.get("priceChange") or {}).get("m5")),
        "price_change_h1": _numar((pair.get("priceChange") or {}).get("h1")),
        "volume_m5": _numar((pair.get("volume") or {}).get("m5")),
        "volume_h1": _numar((pair.get("volume") or {}).get("h1")),
        "buys_m5": int(_numar(tx.get("buys")) or 0),
        "sells_m5": int(_numar(tx.get("sells")) or 0),
        "price_usd": _numar(pair.get("priceUsd")),
        "dex": pair.get("dexId"),
    }


def motiv_rece(m: dict[str, Any], cfg: BotConfig) -> str | None:
    """Motivul pentru care tokenul NU e (inca) fierbinte, sau None daca este."""
    mcap = m.get("market_cap")
    if cfg.min_market_cap_usd > 0:
        if mcap is None:
            return "capitalizare necunoscuta"
        if mcap < cfg.min_market_cap_usd:
            return f"capitalizare ${mcap:,.0f} < ${cfg.min_market_cap_usd:,.0f}"
    if cfg.max_market_cap_usd > 0 and mcap is not None and mcap > cfg.max_market_cap_usd:
        return f"capitalizare ${mcap:,.0f} peste ${cfg.max_market_cap_usd:,.0f} (prea tarziu)"

    schimbare = m.get("price_change_m5")
    if cfg.min_price_change_m5_pct > 0:
        if schimbare is None:
            return "variatie de pret necunoscuta"
        if schimbare < cfg.min_price_change_m5_pct:
            return f"pret {schimbare:+.0f}% in 5 min < +{cfg.min_price_change_m5_pct:.0f}%"

    vol = m.get("volume_m5")
    if cfg.min_volume_m5_usd > 0 and (vol is None or vol < cfg.min_volume_m5_usd):
        return f"volum 5 min ${vol or 0:,.0f} < ${cfg.min_volume_m5_usd:,.0f}"

    vol_h1 = m.get("volume_h1")
    if cfg.min_volume_h1_usd > 0 and (vol_h1 is None or vol_h1 < cfg.min_volume_h1_usd):
        return f"volum pe ora ${vol_h1 or 0:,.0f} < ${cfg.min_volume_h1_usd:,.0f}"

    cumparari_abs = m.get("buys_m5") or 0
    if cfg.min_buys_m5 > 0 and cumparari_abs < cfg.min_buys_m5:
        return f"doar {cumparari_abs} cumparari in 5 min (minim {cfg.min_buys_m5})"

    if cfg.min_buy_sell_ratio_m5 > 0:
        cumparari, vanzari = m.get("buys_m5") or 0, m.get("sells_m5") or 0
        raport = (cumparari / vanzari) if vanzari else float(cumparari)
        if raport < cfg.min_buy_sell_ratio_m5:
            return f"{cumparari} cumparari / {vanzari} vanzari (raport {raport:.1f})"
    return None


class HeatWatcher:
    """Tine token-urile sub observatie si anunta cand unul ia foc."""

    def __init__(
        self,
        cfg: BotConfig,
        http: httpx.AsyncClient,
        on_hot: Callable[[Urmarit, dict[str, Any]], None],
        tracker: Any = None,
    ) -> None:
        self.cfg = cfg
        self.http = http
        self.on_hot = on_hot
        # Sursa rapida: tranzactiile citite direct din loguri. DexScreener
        # ramane doar ca rezerva, pentru token-urile fara activitate recenta.
        self.tracker = tracker
        self.aprinse_live = 0
        self._urmarite: dict[str, Urmarit] = {}
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.aprinse = 0
        self.expirate = 0
        self.evacuate = 0
        self.cicluri = 0
        self.erori = 0

    @property
    def enabled(self) -> bool:
        return self.cfg.heat_watch_seconds > 0

    @property
    def stats(self) -> dict[str, int]:
        return {
            "watching": len(self._urmarite),
            "hot": self.aprinse,
            "expired": self.expirate,
            "evicted": self.evacuate,
            "cycles": self.cicluri,
            "hot_live": self.aprinse_live,
            "errors": self.erori,
        }

    # ------------------------------------------------------------------
    def start(self) -> None:
        if not self.enabled:
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._bucla(), name="heat-watcher")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    def verifica_live(self, mint: str) -> None:
        """Verificare instantanee, la fiecare tranzactie a unui token urmarit.

        Aici se castiga timpul: nu asteptam urmatorul ciclu de interogare, ci
        reactionam in aceeasi milisecunda in care tranzactia a intrat in log.
        """
        if self.tracker is None:
            return
        u = self._urmarite.get(mint)
        if u is None:
            return
        masura = self.tracker.masoara(mint)
        if masura is None or motiv_rece(masura, self.cfg) is not None:
            return
        self._aprinde(u, masura, live=True)

    def _aprinde(self, u: Urmarit, masura: dict[str, Any], live: bool = False) -> None:
        self._urmarite.pop(u.mint, None)
        if self.tracker is not None:
            self.tracker.uita(u.mint)
        self.aprinse += 1
        if live:
            self.aprinse_live += 1
        masura["watched_s"] = round(time.monotonic() - u.adaugat_la)
        masura["reads"] = u.citiri
        try:
            self.on_hot(u, masura)
        except Exception:
            log.exception("Handler-ul de token fierbinte a aruncat")

    def watch(self, mint: str, meta: dict[str, Any], detectat_la: float = 0.0) -> None:
        """Adauga un token sub supraveghere. Non-blocant, nu arunca."""
        if not self.enabled or not mint or mint in self._urmarite:
            return
        # Plafon dur: numarul de cereri catre DexScreener creste liniar cu
        # numarul de token-uri urmarite. Cand se umple, scoatem cel mai vechi -
        # a avut deja cel mai mult timp sa porneasca si n-a pornit.
        while len(self._urmarite) >= self.cfg.heat_max_watched:
            cel_mai_vechi = min(self._urmarite.values(), key=lambda u: u.adaugat_la)
            del self._urmarite[cel_mai_vechi.mint]
            if self.tracker is not None:
                self.tracker.uita(cel_mai_vechi.mint)
            self.evacuate += 1
        self._urmarite[mint] = Urmarit(mint=mint, meta=meta, detectat_la=detectat_la)
        if self.tracker is not None:
            self.tracker.urmareste(mint)

    # ------------------------------------------------------------------
    async def _bucla(self) -> None:
        """Bucla de supraveghere. Nu are voie sa moara."""
        while not self._stop.is_set():
            try:
                await self._un_ciclu()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.erori += 1
                log.exception("Eroare in supravegherea de foc: %s", exc)
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.cfg.heat_poll_interval
                )
                return
            except asyncio.TimeoutError:
                pass

    async def _un_ciclu(self) -> None:
        self.cicluri += 1
        acum = time.monotonic()

        # Scoatem ce a stat prea mult fara sa se aprinda.
        for mint, u in list(self._urmarite.items()):
            if acum - u.adaugat_la > self.cfg.heat_watch_seconds:
                del self._urmarite[mint]
                if self.tracker is not None:
                    self.tracker.uita(mint)
                self.expirate += 1

        if not self._urmarite:
            return

        mints = list(self._urmarite)
        for i in range(0, len(mints), MAX_ADRESE_PE_CERERE):
            lot = mints[i : i + MAX_ADRESE_PE_CERERE]
            await self._verifica_lot(lot)

    async def _verifica_lot(self, lot: list[str]) -> None:
        url = DEXSCREENER_TOKENS.format(mints=",".join(lot))
        try:
            resp = await self.http.get(url)
        except httpx.HTTPError as exc:
            self.erori += 1
            log.debug("Lot DexScreener esuat: %s", exc)
            return
        if resp.status_code != 200:
            if resp.status_code == 429:
                log.warning("DexScreener rate limit; incetinesc un ciclu.")
                await asyncio.sleep(2.0)
            self.erori += 1
            return
        try:
            body = resp.json()
        except ValueError:
            self.erori += 1
            return

        # Un mint poate avea mai multe perechi; o pastram pe cea cu capitalizarea
        # cea mai mare, adica piata principala.
        cele_mai_bune: dict[str, dict[str, Any]] = {}
        for pair in (body or {}).get("pairs") or []:
            if not isinstance(pair, dict):
                continue
            adresa = (pair.get("baseToken") or {}).get("address")
            if adresa not in self._urmarite:
                continue
            m = masoara_pereche(pair)
            veche = cele_mai_bune.get(adresa)
            if veche is None or (m["market_cap"] or 0) > (veche["market_cap"] or 0):
                cele_mai_bune[adresa] = m

        for adresa, masura in cele_mai_bune.items():
            u = self._urmarite.get(adresa)
            if u is None:
                continue
            u.citiri += 1
            u.cap_maxim = max(u.cap_maxim, masura.get("market_cap") or 0.0)
            if motiv_rece(masura, self.cfg) is None:
                masura["sursa"] = "dexscreener"
                self._aprinde(u, masura)


def _numar(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
