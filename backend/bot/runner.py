"""Orchestratorul unei instante de bot.

Ruleaza ca proces separat:
    python -m bot.runner --config configs/default.json

Fluxul:
    logsSubscribe -> semnatura -> getTransaction -> mint/nume/simbol
    -> pipeline anti-rug fail-fast -> eveniment `verdict` catre backend.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .checks import AntiRugPipeline
from .config import BotConfig, ConfigError
from .emitter import EventEmitter
from .events import Event
from .listener import Detection, PumpFunListener
from .logging_setup import get_logger, setup_logging
from .parser import parse_create_logs, parse_create_transaction
from .rpc import RpcError, RpcUnavailable, SolanaRpc
from .settings import BASE_DIR, SETTINGS, mask_endpoint
from .heatwatch import HeatWatcher, Urmarit
from .trades import TradeTracker
from .telegram import TelegramError, TelegramNotifier, discover_chat_id

log = get_logger("bot.runner")

# De cate ori reincercam getTransaction cand nodul nu are inca tranzactia
# (o vedem in loguri la commitment=processed, dar indexarea vine cu intarziere).
# Aceasta e doar calea de rezerva: in mod normal tokenul se extrage din loguri.
TX_FETCH_ATTEMPTS = 3
TX_FETCH_DELAY = 0.6

# Cat de "vechi" poate fi o detectie cand ii vine randul la procesare.
# Peste acest prag o aruncam: un alert care ajunge dupa un minut nu ajuta la
# nimic, iar tinerea lui in coada intarzie token-urile proaspete.
MAX_DETECTION_AGE_S = 25.0

# Cat asteptam verificarile in curs la oprire, inainte sa le anulam.
# Trebuie sa fie confortabil sub GRACEFUL_TIMEOUT-ul managerului (8s + drenajul
# emitentului), altfel botul primeste SIGKILL si pierdem ultimele evenimente.
SHUTDOWN_TASK_TIMEOUT = 4.0

# Cod de iesire pentru "configuratie incompleta, nu reporni degeaba".
# Managerul il trateaza separat de un crash: repornirea n-ar schimba nimic.
EXIT_NEEDS_CONFIG = 3


@dataclass
class BotStats:
    """Contoare pentru dashboard."""

    started_at: float = field(default_factory=time.time)
    detected: int = 0        # tranzactii `create` vazute in loguri
    resolved: int = 0        # tranzactii din care am extras mint-ul
    passed: int = 0          # au trecut tot pipeline-ul
    failed: int = 0          # respinse de un filtru
    pending: int = 0         # fara lichiditate indexata inca
    errors: int = 0          # nu s-a putut decide (RPC/parse)
    dropped: int = 0         # aruncate pentru ca au stat prea mult in coada
    rpc_fallbacks: int = 0   # cate au avut nevoie de getTransaction
    latency_sum_ms: int = 0
    latency_count: int = 0

    def record_latency(self, ms: int) -> None:
        self.latency_sum_ms += ms
        self.latency_count += 1

    def snapshot(self) -> dict:
        avg = round(self.latency_sum_ms / self.latency_count) if self.latency_count else None
        return {
            "uptime_s": round(time.time() - self.started_at, 1),
            "detected": self.detected,
            "resolved": self.resolved,
            "passed": self.passed,
            "failed": self.failed,
            "pending": self.pending,
            "errors": self.errors,
            "dropped": self.dropped,
            "rpc_fallbacks": self.rpc_fallbacks,
            "avg_latency_ms": avg,
        }


class BotRunner:
    def __init__(self, cfg: BotConfig) -> None:
        self.cfg = cfg
        self.stats = BotStats()
        self._stop = asyncio.Event()
        self._tasks: set[asyncio.Task] = set()

        rpc_http = cfg.rpc_http or SETTINGS.rpc_http
        rpc_ws = cfg.rpc_ws or SETTINGS.rpc_ws

        self.rpc = SolanaRpc(
            rpc_http,
            timeout=cfg.http_timeout,
            min_interval=cfg.rpc_min_interval,
            max_retries=cfg.rpc_max_retries,
        )
        self.http = httpx.AsyncClient(
            timeout=httpx.Timeout(cfg.http_timeout, connect=5.0),
            headers={"user-agent": "pumpfun-monitor/1.0 (open-source, non-commercial)"},
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
            follow_redirects=True,
        )
        self.emitter = EventEmitter(SETTINGS.backend_url, SETTINGS.ingest_token)
        # Notificari Telegram - optionale. Fara token, obiectul e inert.
        self.telegram = TelegramNotifier(
            SETTINGS.telegram_bot_token, SETTINGS.telegram_chat_id
        )
        self.pipeline = AntiRugPipeline(cfg, self.rpc, self.http)
        # Supravegherea de foc e COMUNA, nu per token: un singur set de mint-uri
        # interogate in loturi de 30. Asa nu mai tinem sute de sloturi ocupate.
        # Tranzactiile citite direct din loguri: pret si cumparari in timp real.
        self.tracker = TradeTracker(fereastra_secunde=cfg.trade_window_seconds)
        self.heat = HeatWatcher(cfg, self.http, self._token_a_luat_foc, self.tracker)
        self.listener = PumpFunListener(
            cfg,
            rpc_ws,
            on_connect=lambda: self._emit("ws_connected", message="WebSocket conectat"),
            on_disconnect=lambda why: self._emit(
                "ws_disconnected", message=f"WebSocket pierdut: {why}", level="warning"
            ),
            on_trades=self._tranzactii_noi,
        )
        self._sem = asyncio.Semaphore(cfg.max_concurrent_checks)

    # ------------------------------------------------------------------
    def _emit(self, event_type: str, **kwargs) -> None:
        self.emitter.emit(Event(bot_id=self.cfg.bot_id, type=event_type, **kwargs))  # type: ignore[arg-type]

    def request_stop(self) -> None:
        if not self._stop.is_set():
            log.info("[%s] Oprire ceruta, inchid curat...", self.cfg.bot_id)
        self._stop.set()
        self.listener.stop()

    # ------------------------------------------------------------------
    async def run(self) -> int:
        """Returneaza codul de iesire al procesului."""
        self.emitter.start()
        self.telegram.start()
        self.heat.start()
        if self.heat.enabled:
            log.info(
                "[%s] Supraveghere de foc activa: %.0f min, verificare la %.0fs",
                self.cfg.bot_id, self.cfg.heat_watch_seconds / 60,
                self.cfg.heat_poll_interval,
            )
        if self.telegram.enabled:
            log.info("[%s] Notificari Telegram active", self.cfg.bot_id)
        self._emit(
            "bot_started",
            message=f"Bot '{self.cfg.label}' pornit",
            data={
                "config": self.cfg.to_dict(),
                "rpc_http": mask_endpoint(self.cfg.rpc_http or SETTINGS.rpc_http),
                "rpc_ws": mask_endpoint(self.cfg.rpc_ws or SETTINGS.rpc_ws),
            },
        )

        self._warn_if_public_rpc()

        health = await self.rpc.get_health()
        if health != "ok":
            log.warning("[%s] RPC raporteaza: %s", self.cfg.bot_id, health)
            self._emit("error", message=f"RPC health: {health}", level="warning")

        heartbeat = asyncio.create_task(self._heartbeat(), name="heartbeat")
        exit_code = 0
        try:
            async for detection in self.listener.detections():
                self.stats.detected += 1
                self._emit(
                    "token_detected",
                    signature=detection.signature,
                    message="Instruction: Create detectata",
                )
                self._spawn(self._process(detection))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("[%s] Eroare fatala in bucla principala", self.cfg.bot_id)
            self._emit("error", message=f"Eroare fatala: {exc}", level="error")
            exit_code = 1
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            await self._shutdown(exit_code)
        return exit_code

    def _warn_if_public_rpc(self) -> None:
        """Avertizeaza daca rulam pe RPC-ul public, unde 429-urile sunt inevitabile."""
        endpoint = self.cfg.rpc_http or SETTINGS.rpc_http
        if "api.mainnet-beta.solana.com" not in endpoint:
            return
        message = (
            "Folosesti RPC-ul public Solana. Are rate limit strans: cu mai multi boti "
            "porniti simultan vei primi HTTP 429 si vei vedea verdicte ERROR. "
            "Pentru rulare pe termen lung foloseste un provider gratuit dedicat "
            "(ex. Helius free tier) in SOLANA_RPC_HTTP / SOLANA_RPC_WS."
        )
        log.warning("[%s] %s", self.cfg.bot_id, message)
        self._emit("error", message=message, level="warning")

    def _spawn(self, coro) -> None:
        """Porneste o verificare in fundal si tine referinta (altfel GC o poate ucide)."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _shutdown(self, exit_code: int) -> None:
        # Lasam verificarile in curs sa se termine, dar nu la nesfarsit: managerul
        # ne acorda GRACEFUL_TIMEOUT secunde, iar dupa asta mai trebuie sa golim
        # coada de evenimente. O verificare cu asteptare de lichiditate poate dura
        # zeci de secunde, deci o taiem.
        if self._tasks:
            log.info("[%s] Astept %d verificari in curs...", self.cfg.bot_id, len(self._tasks))
            # Copiem setul: done-callback-urile il modifica in timp ce asteptam.
            done, pending = await asyncio.wait(list(self._tasks), timeout=SHUTDOWN_TASK_TIMEOUT)
            for task in pending:
                task.cancel()

        self._emit(
            "bot_stopped",
            message="Bot oprit",
            level="warning" if exit_code else "info",
            data=self.stats.snapshot(),
        )
        await self.heat.stop()
        await self.emitter.stop()
        await self.telegram.stop()
        await self.rpc.aclose()
        await self.http.aclose()

    # ------------------------------------------------------------------
    async def _heartbeat(self) -> None:
        """Trimite periodic contoarele, ca dashboardul sa arate uptime real."""
        cicluri = 0
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=15.0)
                return
            except asyncio.TimeoutError:
                pass

            # Pretul SOL la fiecare ~5 minute (heartbeat-ul e la 15 secunde).
            cicluri += 1
            if self.heat.enabled and cicluri % 20 == 1:
                await self._actualizeaza_pret_sol()

            self._emit(
                "stats",
                data={
                    **self.stats.snapshot(),
                    "ws_connected": self.listener.connected,
                    "reconnects": self.listener.reconnects,
                    "logs_seen": self.listener.logs_received,
                    "emitter": self.emitter.stats,
                    "telegram": self.telegram.stats if self.telegram.enabled else None,
                    "heat": self.heat.stats if self.heat.enabled else None,
                    "trades": {
                        "seen": self.tracker.tranzactii_vazute,
                        "kept": self.tracker.tranzactii_retinute,
                        "tracked": self.tracker.urmarite,
                    } if self.heat.enabled else None,
                    "inflight": len(self._tasks),
                },
            )

    # ------------------------------------------------------------------
    async def _process(self, detection: Detection) -> None:
        """Pipeline complet pentru o singura detectie."""
        signature = detection.signature
        detected_at = detection.detected_at

        # Calea rapida: token-ul e deja in logurile primite pe WebSocket.
        # Zero apeluri RPC => verdictul depinde doar de un getAccountInfo.
        info = parse_create_logs(detection.logs)

        def _too_old() -> bool:
            return time.monotonic() - detected_at > MAX_DETECTION_AGE_S

        def _drop(age: float) -> None:
            self.stats.dropped += 1
            log.warning(
                "[%s] Arunc detectia %s (a stat %.1fs in coada, %d in lucru)",
                self.cfg.bot_id, signature[:12], age, len(self._tasks),
            )
            self._emit(
                "verdict", signature=signature, mint=info.mint if info else None,
                symbol=info.symbol if info else None,
                verdict="ERROR", reason=f"aruncat: coada aglomerata ({age:.0f}s vechime)",
                failed_stage="queue", level="warning", latency_ms=int(age * 1000),
            )

        # Verificam vechimea INAINTE de semafor. Altfel, cand toate sloturile
        # sunt ocupate (ex. furtuna de 429), detectiile noi nici nu apucau sa fie
        # evaluate pentru a fi aruncate - asteptau la rand ca sa fie declarate
        # expirate, blocand pipeline-ul exact cand era mai incarcat.
        if _too_old():
            _drop(time.monotonic() - detected_at)
            return

        async with self._sem:
            # Recontrolam: intre timp am putut astepta la coada semaforului.
            if _too_old():
                _drop(time.monotonic() - detected_at)
                return

            try:
                if info is None:
                    self.stats.rpc_fallbacks += 1
                    info = await self._resolve_token(signature)
            except (RpcError, RpcUnavailable) as exc:
                self.stats.errors += 1
                self._emit(
                    "verdict", signature=signature, verdict="ERROR",
                    reason=f"nu am putut citi tranzactia: {exc}",
                    failed_stage="fetch_tx", level="warning",
                    latency_ms=_elapsed_ms(detected_at),
                )
                return
            except Exception as exc:
                self.stats.errors += 1
                log.exception("[%s] Eroare la rezolvarea %s", self.cfg.bot_id, signature[:16])
                self._emit(
                    "verdict", signature=signature, verdict="ERROR",
                    reason=f"eroare interna: {type(exc).__name__}", failed_stage="fetch_tx",
                    level="error", latency_ms=_elapsed_ms(detected_at),
                )
                return

            if info is None:
                self.stats.errors += 1
                self._emit(
                    "verdict", signature=signature, verdict="ERROR",
                    reason="tranzactia nu contine un token pump.fun valid",
                    failed_stage="parse", level="warning",
                    latency_ms=_elapsed_ms(detected_at),
                )
                return

            self.stats.resolved += 1
            self._emit(
                "token_resolved",
                signature=signature, mint=info.mint, name=info.name,
                symbol=info.symbol, uri=info.uri, creator=info.creator,
                data={"parser": info.source, "resolve_ms": _elapsed_ms(detected_at)},
            )

            try:
                # Deadline absolut, calculat de la momentul detectiei: pipeline-ul
                # se opreste singur daca RPC-ul e prea lent, in loc sa produca
                # verdicte de zeci de secunde care nu mai folosesc la nimic.
                deadline = detected_at + MAX_DETECTION_AGE_S + self._slow_stage_budget()
                result = await self.pipeline.run(info.mint, deadline=deadline)
            except Exception as exc:
                self.stats.errors += 1
                log.exception("[%s] Pipeline crapat pentru %s", self.cfg.bot_id, info.mint)
                self._emit(
                    "verdict", signature=signature, mint=info.mint, name=info.name,
                    symbol=info.symbol, verdict="ERROR",
                    reason=f"pipeline: {type(exc).__name__}: {exc}",
                    failed_stage="pipeline", level="error",
                    latency_ms=_elapsed_ms(detected_at),
                )
                return

            latency = _elapsed_ms(detected_at)
            self.stats.record_latency(latency)
            counter = {
                "PASS": "passed", "FAIL": "failed",
                "PENDING": "pending", "ERROR": "errors",
            }.get(result.verdict, "errors")
            setattr(self.stats, counter, getattr(self.stats, counter) + 1)

            log.info(
                "[%s] %-7s %-12s %s  (%dms) - %s",
                self.cfg.bot_id, result.verdict, (info.symbol or "?")[:12],
                info.mint[:8], latency, result.reason,
            )

            # Tokenul a trecut verificarile de siguranta. Daca supravegherea de
            # foc e pornita, NU alertam acum - il dam supraveghetorului, care
            # trimite abia cand chiar ia foc. Altfel, alerta pleaca imediat.
            if result.verdict == "PASS":
                if self.heat.enabled:
                    self.heat.watch(
                        info.mint,
                        {"name": info.name, "symbol": info.symbol, "signature": signature},
                        detectat_la=detected_at,
                    )
                elif self.cfg.telegram_notify:
                    self._trimite_alerta(info.mint, info.name, info.symbol, latency, None)

            self._emit(
                "verdict",
                signature=signature, mint=info.mint, name=info.name, symbol=info.symbol,
                uri=info.uri, creator=info.creator,
                verdict=result.verdict, reason=result.reason,
                failed_stage=result.failed_stage, latency_ms=latency,
                checks={**result.as_checks_dict(), "resolver": info.source},
                level="info" if result.verdict == "PASS" else "warning",
            )

    async def _actualizeaza_pret_sol(self) -> None:
        """Pretul SOL in dolari, ca sa putem exprima capitalizarea in USD.

        O cerere la cateva minute. Daca esueaza, pastram ultima valoare buna -
        o capitalizare usor invechita e mult mai buna decat niciuna.
        """
        try:
            r = await self.http.get(
                "https://api.dexscreener.com/latest/dex/tokens/"
                "So11111111111111111111111111111111111111112"
            )
            if r.status_code != 200:
                return
            perechi = (r.json() or {}).get("pairs") or []
        except Exception as exc:
            log.debug("[%s] Nu am putut afla pretul SOL: %s", self.cfg.bot_id, exc)
            return

        preturi = []
        for p in perechi:
            if not isinstance(p, dict):
                continue
            val = p.get("priceUsd")
            try:
                val = float(val)
            except (TypeError, ValueError):
                continue
            if 10 < val < 10000:      # santinela impotriva perechilor aiurea
                preturi.append(val)
        if preturi:
            preturi.sort()
            self.tracker.pret_sol_usd = preturi[len(preturi) // 2]
            log.debug("[%s] Pret SOL: $%.2f", self.cfg.bot_id, self.tracker.pret_sol_usd)

    def _tranzactii_noi(self, tranzactii: list) -> None:
        """Fiecare tranzactie de pe pump.fun trece pe aici, in timp real.

        Retinem doar token-urile supravegheate si, imediat ce unul depaseste
        pragurile, plecam cu alerta - fara sa asteptam urmatoarea interogare.
        """
        atinse = set()
        for t in tranzactii:
            if self.tracker.adauga(t):
                atinse.add(t.mint)
        for mint in atinse:
            self.heat.verifica_live(mint)

    def _token_a_luat_foc(self, u: "Urmarit", masura: dict) -> None:
        """Apelat de supraveghetor cand un token indeplineste conditiile."""
        nume = u.meta.get("name") or ""
        simbol = u.meta.get("symbol") or ""
        de_la_detectie = (
            int((time.monotonic() - u.detectat_la) * 1000) if u.detectat_la else None
        )

        log.info(
            "[%s] FOC     %-12s %s  cap $%s, %+.0f%% in 5min, %d/%d  (dupa %ss)",
            self.cfg.bot_id, (simbol or "?")[:12], u.mint[:8],
            f"{masura.get('market_cap') or 0:,.0f}", masura.get("price_change_m5") or 0,
            masura.get("buys_m5") or 0, masura.get("sells_m5") or 0,
            masura.get("watched_s"),
        )

        self._emit(
            "hot",
            mint=u.mint, name=nume, symbol=simbol,
            signature=u.meta.get("signature"),
            verdict="PASS",
            reason=(
                f"a luat foc: ${masura.get('market_cap') or 0:,.0f} cap., "
                f"{masura.get('price_change_m5') or 0:+.0f}% in 5 min, "
                f"{masura.get('buys_m5')} cumparari / {masura.get('sells_m5')} vanzari"
            ),
            latency_ms=de_la_detectie,
            checks={"heat": masura},
        )

        if self.cfg.telegram_notify:
            self._trimite_alerta(u.mint, nume, simbol, de_la_detectie, masura)

    def _trimite_alerta(
        self, mint: str, nume: str, simbol: str, latenta: int | None, masura: dict | None
    ) -> None:
        """Notificare Telegram. Nu are voie sa arunce catre apelant."""
        try:
            self.telegram.notify_pass(
                bot_id=self.cfg.bot_id, name=nume, symbol=simbol,
                mint=mint, latency_ms=latenta, heat=masura,
            )
        except Exception as exc:
            log.warning("[%s] Notificarea Telegram a esuat: %s", self.cfg.bot_id, exc)

    def _slow_stage_budget(self) -> float:
        """Timp suplimentar acordat etapelor externe (asteptarea de lichiditate)."""
        buget = 0.0
        if self.cfg.check_liquidity:
            buget += self.cfg.liquidity_wait_seconds + 5.0
        # Supravegherea de foc NU mai tine slotul ocupat - ruleaza separat.
        return buget

    async def _resolve_token(self, signature: str):
        """getTransaction cu cateva reincercari (indexarea are latenta)."""
        for attempt in range(TX_FETCH_ATTEMPTS):
            tx = await self.rpc.get_transaction(signature)
            if tx is not None:
                return parse_create_transaction(tx)
            if attempt < TX_FETCH_ATTEMPTS - 1:
                await asyncio.sleep(TX_FETCH_DELAY * (attempt + 1))
        log.debug("[%s] Tranzactia %s nu a fost gasita de nod", self.cfg.bot_id, signature[:16])
        return None


def _elapsed_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


# ----------------------------------------------------------------------
async def _print_chat_id_and_exit() -> int:
    """Afiseaza chat_id-ul gasit prin getUpdates si opreste procesul.

    Cerinta explicita: nu ghicim si nu trimitem nimic fara un chat_id pus de om
    in .env. Deci il cautam, il afisam clar si ne oprim aici.
    """
    log.info("TELEGRAM_BOT_TOKEN e setat, dar TELEGRAM_CHAT_ID lipseste. Il caut...")
    try:
        chats = await discover_chat_id(SETTINGS.telegram_bot_token)
    except TelegramError as exc:
        log.error("Nu am putut interoga Telegram: %s", exc)
        _print_box([
            "Nu am putut afla chat_id-ul.",
            f"Motiv: {exc}",
            "",
            "Verifica TELEGRAM_BOT_TOKEN in backend/.env.",
        ])
        return EXIT_NEEDS_CONFIG

    if not chats:
        _print_box([
            "Botul nu a primit inca niciun mesaj, deci nu am de unde sa aflu chat_id-ul.",
            "",
            "Deschide Telegram, cauta botul tau si trimite-i un mesaj (orice, ex. 'salut').",
            "Telegram nu lasa un bot sa scrie primul cuiva care nu i-a scris niciodata.",
            "Apoi porneste botul din nou.",
        ])
        return EXIT_NEEDS_CONFIG

    lines = ["Am gasit chat-urile de mai jos. Copiaza id-ul in backend/.env:", ""]
    for chat in chats:
        eticheta = chat.get("title") or chat.get("from") or chat.get("type") or "?"
        lines.append(f"    TELEGRAM_CHAT_ID={chat['chat_id']}    <- {eticheta} ({chat.get('type')})")
    lines += ["", "Apoi porneste botul din nou. Pana atunci nu trimit nimic."]
    _print_box(lines)
    return EXIT_NEEDS_CONFIG


def _print_box(lines: list[str]) -> None:
    """Scrie un bloc vizibil pe stderr - trebuie sa sara in ochi in loguri."""
    width = max(len(line) for line in lines) + 4
    border = "=" * width
    print(f"\n{border}", file=sys.stderr)
    for line in lines:
        print(f"  {line}", file=sys.stderr)
    print(f"{border}\n", file=sys.stderr, flush=True)


async def _main_async(cfg: BotConfig) -> int:
    # Inainte de orice: daca avem token dar nu avem chat_id, aflam id-ul,
    # il afisam si iesim. Nu pornim WebSocket-ul degeaba.
    if SETTINGS.telegram_bot_token and not SETTINGS.telegram_chat_id:
        return await _print_chat_id_and_exit()

    runner = BotRunner(cfg)
    loop = asyncio.get_running_loop()

    # SIGINT/SIGTERM -> oprire curata (flush evenimente, inchidere sockets).
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, ValueError):
            loop.add_signal_handler(sig, runner.request_stop)

    return await runner.run()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bot de monitorizare pump.fun")
    parser.add_argument("--config", required=True, help="cale catre fisierul JSON de config")
    parser.add_argument("--log-level", default=None, help="DEBUG/INFO/WARNING/ERROR")
    args = parser.parse_args(argv)

    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = BASE_DIR / config_path

    try:
        cfg = BotConfig.load(config_path)
    except ConfigError as exc:
        print(f"Config invalid: {exc}", file=sys.stderr)
        return 2

    setup_logging(
        args.log_level or SETTINGS.log_level,
        log_file=BASE_DIR / "data" / "logs" / f"{cfg.bot_id}.log",
    )
    log.info("Pornesc botul '%s' cu config %s", cfg.bot_id, config_path.name)

    try:
        return asyncio.run(_main_async(cfg))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
