"""Configuratie per-instanta de bot (un fisier JSON = un bot)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from .settings import BASE_DIR, mask_endpoint

CONFIG_DIR = BASE_DIR / "configs"

# bot_id-ul ajunge in nume de fisiere si in URL-uri -> il tinem strict.
_BOT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")


class ConfigError(ValueError):
    """Configuratie invalida - aruncata cu mesaj citibil de om."""


@dataclass
class BotConfig:
    """Praguri si optiuni pentru o singura instanta de bot."""

    bot_id: str
    label: str = ""

    # --- Filtre on-chain (pasul a: cel mai rapid, gratis) ---
    require_mint_authority_revoked: bool = True
    require_freeze_authority_revoked: bool = True
    # Contul de mint apare cu cateva sute de ms dupa log; nodurile din spatele
    # RPC-ului public sunt si ele desincronizate. Reincercam de cateva ori
    # inainte sa declaram ca mint-ul nu exista.
    authority_retries: int = 5
    authority_retry_delay: float = 0.4
    # Commitment pentru getAccountInfo: processed (rapid) sau confirmed (sigur).
    authority_commitment: str = "processed"

    # --- Filtru lichiditate DexScreener (pasul b) ---
    check_liquidity: bool = True
    min_liquidity_usd: float = 1000.0
    # Token-urile abia lansate n-au inca pereche pe DexScreener. Cate secunde
    # asteptam (cu re-interogare) inainte sa decidem ca nu exista lichiditate.
    liquidity_wait_seconds: float = 8.0
    liquidity_poll_interval: float = 4.0
    # Praguri de TRACTIUNE reala, din DexScreener. Pentru un token pump.fun
    # proaspat, `liquidity` lipseste (e pe bonding curve), dar `fdv` si volumul
    # exista si sunt singurele semnale care chiar separa: mediana volumului la
    # 3 minute e ~$26, in timp ce lansarile care conteaza fac mii de dolari.
    # 0 = prag dezactivat. Cere `liquidity_wait_seconds` >= ~90s, fiindca
    # DexScreener indexeaza perechea abia dupa ~60-70 secunde.
    min_fdv_usd: float = 0.0
    min_volume_h24_usd: float = 0.0

    # Daca True, un token a carui lichiditate nu poate fi aflata NU e respins.
    # Implicit True: DexScreener nu raporteaza lichiditate cat timp tokenul e pe
    # bonding curve, adica exact in fereastra in care botul il vede. Cu False,
    # aproape orice lansare noua ar iesi PENDING.
    allow_missing_liquidity: bool = True

    # --- Supraveghere de "IA FOC" (pasul d) ---
    # Nu prezice nimic. Urmareste tokenul si trimite alerta EXACT cand devine
    # fierbinte: capitalizare peste prag, pret in urcare, mult mai multe
    # cumparari decat vanzari si volum real. Masurat pe date live: pe pump.fun
    # capitalizarea de start e ~$2.600 si acolo raman ~29 din 30 de token-uri;
    # cele care chiar pornesc sar la $10k+ cu +100% in 5 minute.
    # 0 = supraveghere dezactivata.
    heat_watch_seconds: float = 0.0
    heat_poll_interval: float = 30.0
    # Plafon de token-uri sub observatie simultan. Conteaza pentru ca numarul
    # de cereri catre DexScreener e `urmarite / 30` la fiecare ciclu: 900 de
    # token-uri la 10 secunde = 3 cereri/s. Cand se umple, cele mai vechi ies
    # primele (oricum sunt cele care n-au pornit).
    heat_max_watched: int = 900
    # Fereastra pe care se calculeaza indicatorii din tranzactiile live.
    # Mai scurta = reactie mai rapida, dar mai zgomotoasa.
    trade_window_seconds: float = 60.0
    # Capitalizare minima ca sa nu primesti alerte despre token-uri moarte.
    min_market_cap_usd: float = 0.0
    # Capitalizare maxima, ca sa fii inca devreme. 0 = fara plafon.
    max_market_cap_usd: float = 0.0
    # Cat trebuie sa urce pretul in ultimele 5 minute.
    min_price_change_m5_pct: float = 0.0
    # De cate ori mai multe cumparari decat vanzari (1.5 = cu 50% mai multe).
    min_buy_sell_ratio_m5: float = 0.0
    # Volum minim in ultimele 5 minute, ca sa nu fie un token inghetat.
    min_volume_m5_usd: float = 0.0
    # Numar ABSOLUT de cumparari in ultimele 5 minute. Raportul singur nu ajunge:
    # "3 cumparari / 0 vanzari" trece orice raport, dar nu inseamna nimic.
    # Asta separa interesul real de doua tranzactii intre aceleasi portofele.
    min_buys_m5: int = 0
    # Volum pe ultima ora: activitate SUSTINUTA, nu un varf de 5 minute care
    # se stinge imediat. Filtrul principal impotriva token-urilor "inghetate".
    min_volume_h1_usd: float = 0.0

    # --- Filtru RugCheck (pasul c: a doua opinie) ---
    check_rugcheck: bool = True
    # Scorul RugCheck: mai mic = mai sigur. Peste prag => respins.
    max_rugcheck_score: float = 2500.0
    # Daca RugCheck e down / rate-limited, tratam tokenul ca trecut (nu-l pierdem).
    rugcheck_fail_open: bool = True
    # RugCheck nu are raport pentru token-uri de cateva secunde: raspunde
    # 400 "unable to generate report". Cat asteptam sa apara raportul.
    # 0 = nu asteptam deloc (a doua opinie lipseste pentru lansari foarte noi).
    rugcheck_wait_seconds: float = 0.0
    rugcheck_poll_interval: float = 3.0

    # --- Timpi si rate limiting ---
    http_timeout: float = 10.0
    # Cate token-uri pot fi verificate in paralel. Munca e I/O-bound (asteptam
    # retea), iar apelurile RPC sunt oricum serializate de rate limiter, deci
    # o valoare mare nu incarca CPU-ul - dar impiedica formarea unei cozi cand
    # etapa de lichiditate asteapta zeci de secunde dupa indexare.
    max_concurrent_checks: int = 24
    # Pauza minima intre doua apeluri RPC. ATENTIE: e per instanta de bot, iar
    # rate limit-ul furnizorului e per IP. Cu N boti pe acelasi RPC public,
    # incarcarea reala e de N ori mai mare - creste valoarea sau treci pe Helius.
    rpc_min_interval: float = 0.5
    # Cate retry-uri pe apel RPC inainte sa renuntam la un token.
    rpc_max_retries: int = 3

    # --- Reconectare WebSocket ---
    ws_backoff_initial: float = 1.0
    ws_backoff_max: float = 60.0
    ws_ping_interval: float = 20.0
    ws_ping_timeout: float = 20.0

    # --- Override-uri RPC per instanta (gol => se ia din .env) ---
    rpc_http: str = ""
    rpc_ws: str = ""

    # Trimite notificari Telegram pentru token-urile care trec? Dashboardul
    # primeste oricum totul; asta controleaza doar alertele pe telefon, ca sa
    # poti tine instante "zgomotoase" fara sa fii spamat.
    telegram_notify: bool = True

    # Metadate libere pentru dashboard
    notes: str = ""
    tags: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------
    def validate(self) -> None:
        """Verifica invarianti. Ridica ConfigError cu mesaj clar."""
        if not _BOT_ID_RE.match(self.bot_id or ""):
            raise ConfigError(
                f"bot_id invalid: {self.bot_id!r}. Permise: litere mici, cifre, '-' si '_', 2-32 caractere."
            )
        if self.min_liquidity_usd < 0:
            raise ConfigError("min_liquidity_usd nu poate fi negativ")
        if self.min_fdv_usd < 0:
            raise ConfigError("min_fdv_usd nu poate fi negativ")
        if self.min_volume_h24_usd < 0:
            raise ConfigError("min_volume_h24_usd nu poate fi negativ")
        if self.liquidity_wait_seconds < 0 or self.liquidity_wait_seconds > 600:
            raise ConfigError("liquidity_wait_seconds trebuie sa fie intre 0 si 600")
        if self.liquidity_poll_interval <= 0:
            raise ConfigError("liquidity_poll_interval trebuie sa fie > 0")
        if self.heat_watch_seconds < 0 or self.heat_watch_seconds > 86400:
            raise ConfigError("heat_watch_seconds trebuie sa fie intre 0 si 86400")
        if not 5 <= self.trade_window_seconds <= 600:
            raise ConfigError("trade_window_seconds trebuie sa fie intre 5 si 600")
        if not 1 <= self.heat_max_watched <= 10000:
            raise ConfigError("heat_max_watched trebuie sa fie intre 1 si 10000")
        if self.heat_poll_interval <= 0:
            raise ConfigError("heat_poll_interval trebuie sa fie > 0")
        if self.min_volume_m5_usd < 0:
            raise ConfigError("min_volume_m5_usd nu poate fi negativ")
        if self.min_buys_m5 < 0:
            raise ConfigError("min_buys_m5 nu poate fi negativ")
        if self.min_volume_h1_usd < 0:
            raise ConfigError("min_volume_h1_usd nu poate fi negativ")
        if self.max_market_cap_usd and self.max_market_cap_usd < self.min_market_cap_usd:
            raise ConfigError("max_market_cap_usd trebuie sa fie >= min_market_cap_usd")
        if self.rugcheck_wait_seconds < 0 or self.rugcheck_wait_seconds > 600:
            raise ConfigError("rugcheck_wait_seconds trebuie sa fie intre 0 si 600")
        if self.rugcheck_poll_interval <= 0:
            raise ConfigError("rugcheck_poll_interval trebuie sa fie > 0")
        if self.http_timeout <= 0:
            raise ConfigError("http_timeout trebuie sa fie > 0")
        if not 1 <= self.max_concurrent_checks <= 128:
            raise ConfigError("max_concurrent_checks trebuie sa fie intre 1 si 128")
        if self.rpc_min_interval < 0:
            raise ConfigError("rpc_min_interval nu poate fi negativ")
        if self.rpc_max_retries < 0:
            raise ConfigError("rpc_max_retries nu poate fi negativ")
        if not 0 <= self.authority_retries <= 20:
            raise ConfigError("authority_retries trebuie sa fie intre 0 si 20")
        if self.authority_retry_delay < 0:
            raise ConfigError("authority_retry_delay nu poate fi negativ")
        if self.authority_commitment not in ("processed", "confirmed", "finalized"):
            raise ConfigError(
                "authority_commitment trebuie sa fie processed, confirmed sau finalized"
            )
        if self.ws_backoff_initial <= 0 or self.ws_backoff_max < self.ws_backoff_initial:
            raise ConfigError("backoff invalid: initial > 0 si max >= initial")
        if not self.label:
            self.label = self.bot_id

    # ------------------------------------------------------------------
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BotConfig":
        """Construieste din dict ignorand cheile necunoscute (config forward-compatible)."""
        if not isinstance(data, dict):
            raise ConfigError("configuratia trebuie sa fie un obiect JSON")

        known = {f for f in cls.__dataclass_fields__}
        unknown = set(data) - known
        clean = {k: v for k, v in data.items() if k in known}
        try:
            cfg = cls(**clean)
        except TypeError as exc:  # lipseste bot_id sau tip gresit
            raise ConfigError(f"configuratie invalida: {exc}") from exc

        cfg.validate()
        if unknown:
            cfg.notes = (cfg.notes + f" [chei ignorate: {', '.join(sorted(unknown))}]").strip()
        return cfg

    @classmethod
    def load(cls, path: Path) -> "BotConfig":
        """Citeste un fisier JSON de config, cu erori explicite."""
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise ConfigError(f"fisierul de config nu exista: {path}") from exc
        except OSError as exc:
            raise ConfigError(f"nu pot citi {path}: {exc}") from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"JSON invalid in {path.name} (linia {exc.lineno}): {exc.msg}") from exc

        # Daca lipseste bot_id, il deducem din numele fisierului.
        if isinstance(data, dict):
            data.setdefault("bot_id", path.stem)
        return cls.from_dict(data)

    def to_dict(self, *, reveal_secrets: bool = False) -> dict[str, Any]:
        """Reprezentare pentru transport (API, WebSocket, evenimente).

        Implicit MASCHEAZA endpoint-urile RPC: pot contine cheia unui provider
        (ex. Helius), iar acest dict ajunge in dashboard, in SQLite si in
        raspunsurile REST. Doar scrierea pe disc cere valorile reale.
        """
        data = asdict(self)
        if not reveal_secrets:
            for key in ("rpc_http", "rpc_ws"):
                if data.get(key):
                    data[key] = mask_endpoint(data[key])
        return data

    def save(self, path: Path | None = None) -> Path:
        target = path or (CONFIG_DIR / f"{self.bot_id}.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        # Scriere atomica: temp + rename, ca sa nu ramana un JSON trunchiat.
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(self.to_dict(reveal_secrets=True), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(target)
        return target


def list_configs() -> list[BotConfig]:
    """Toate configurile valide din configs/. Cele stricate sunt sarite, nu opresc serverul."""
    out: list[BotConfig] = []
    if not CONFIG_DIR.exists():
        return out
    for path in sorted(CONFIG_DIR.glob("*.json")):
        try:
            out.append(BotConfig.load(path))
        except ConfigError:
            continue
    return out


def config_path_for(bot_id: str) -> Path:
    """Calea fisierului de config, cu protectie impotriva path traversal."""
    if not _BOT_ID_RE.match(bot_id or ""):
        raise ConfigError(f"bot_id invalid: {bot_id!r}")
    return CONFIG_DIR / f"{bot_id}.json"
