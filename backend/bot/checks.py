"""Pipeline anti-rug FAIL-FAST.

Ordinea e aleasa dupa cost: intai ce e gratis si instant (on-chain), apoi
API-uri externe. La primul esec ne oprim - nu mai irosim timp si rate limit.

  a) Mint Authority + Freeze Authority  -> getAccountInfo (on-chain, ~50ms)
  b) Lichiditate                        -> DexScreener
  c) RugCheck                           -> a doua opinie
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import BotConfig
from .logging_setup import get_logger
from .parser import parse_mint_account
from .rpc import RpcError, RpcUnavailable, SolanaRpc

log = get_logger("bot.checks")

DEXSCREENER_URL = "https://api.dexscreener.com/latest/dex/tokens/{mint}"
RUGCHECK_URL = "https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary"


@dataclass
class StageResult:
    """Rezultatul unei singure etape din pipeline."""

    stage: str
    passed: bool
    reason: str = ""
    duration_ms: int = 0
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class PipelineResult:
    """Rezultatul complet: verdict + toate etapele rulate."""

    verdict: str  # PASS / FAIL / PENDING / ERROR
    reason: str
    failed_stage: str | None
    stages: list[StageResult]

    def as_checks_dict(self) -> dict[str, Any]:
        return {
            s.stage: {
                "passed": s.passed,
                "reason": s.reason,
                "ms": s.duration_ms,
                **s.details,
            }
            for s in self.stages
        }


class AntiRugPipeline:
    """Ruleaza verificarile pentru un mint. Reutilizabil intre token-uri."""

    def __init__(self, cfg: BotConfig, rpc: SolanaRpc, http: httpx.AsyncClient) -> None:
        self.cfg = cfg
        self.rpc = rpc
        self.http = http

    # ------------------------------------------------------------------
    async def run(self, mint: str, deadline: float | None = None) -> PipelineResult:
        """Ruleaza etapele in ordine, oprindu-se la primul esec.

        `deadline` e un timestamp `time.monotonic()` peste care nu mai are rost
        sa continuam: un verdict care ajunge dupa un minut nu mai foloseste
        nimanui, iar apelurile suplimentare doar consuma bugetul de RPC.
        """
        stages: list[StageResult] = []

        if _expired(deadline):
            return PipelineResult(
                "ERROR", "expirat inainte de prima verificare (RPC supraincarcat)",
                "deadline", stages,
            )

        # --- a) autoritati on-chain -----------------------------------
        stage = await self._check_authorities(mint, deadline)
        stages.append(stage)
        if not stage.passed:
            return PipelineResult(
                verdict="ERROR" if stage.details.get("inconclusive") else "FAIL",
                reason=stage.reason,
                failed_stage=stage.stage,
                stages=stages,
            )

        if _expired(deadline):
            return PipelineResult(
                "ERROR", "expirat dupa verificarea autoritatilor", "deadline", stages
            )

        # --- b) lichiditate DexScreener -------------------------------
        if self.cfg.check_liquidity:
            stage = await self._check_liquidity(mint, deadline)
            stages.append(stage)
            if not stage.passed:
                verdict = "PENDING" if stage.details.get("pending") else "FAIL"
                return PipelineResult(verdict, stage.reason, stage.stage, stages)

        if _expired(deadline):
            return PipelineResult(
                "ERROR", "expirat dupa verificarea lichiditatii", "deadline", stages
            )

        # --- c) RugCheck (a doua opinie) ------------------------------
        if self.cfg.check_rugcheck:
            stage = await self._check_rugcheck(mint, deadline)
            stages.append(stage)
            if not stage.passed:
                return PipelineResult("FAIL", stage.reason, stage.stage, stages)

        return PipelineResult("PASS", "toate verificarile au trecut", None, stages)

    # ------------------------------------------------------------------
    async def _check_authorities(self, mint: str, deadline: float | None = None) -> StageResult:
        """a) Cel mai rapid si gratuit filtru: autoritatile mint-ului."""
        t0 = time.monotonic()

        def done(passed: bool, reason: str, **details: Any) -> StageResult:
            return StageResult(
                "authority", passed, reason, int((time.monotonic() - t0) * 1000), details
            )

        # Contul poate lipsi la prima incercare (nu s-a propagat inca).
        value = None
        attempts = 0
        for attempt in range(self.cfg.authority_retries + 1):
            attempts = attempt + 1
            try:
                value = await self.rpc.get_account_info_parsed(
                    mint, commitment=self.cfg.authority_commitment, deadline=deadline
                )
            except (RpcError, RpcUnavailable) as exc:
                # Nu putem decide -> ERROR, nu FAIL (nu acuzam un token degeaba).
                return done(
                    False, f"RPC indisponibil la getAccountInfo: {exc}",
                    inconclusive=True, attempts=attempts,
                )
            if value is not None:
                break
            # Nu mai insistam daca oricum am depasit fereastra utila.
            if attempt < self.cfg.authority_retries and not _expired(deadline):
                await asyncio.sleep(self.cfg.authority_retry_delay)
            else:
                break

        if value is None:
            return done(
                False, f"contul de mint nu exista on-chain (dupa {attempts} incercari)",
                inconclusive=True, attempts=attempts,
            )

        info = parse_mint_account(value)
        if info is None:
            return done(False, "contul nu e un mint SPL parsabil", inconclusive=True,
                        attempts=attempts)

        mint_auth = info.get("mint_authority")
        freeze_auth = info.get("freeze_authority")
        details = {
            "mint_authority": mint_auth,
            "freeze_authority": freeze_auth,
            "supply": info.get("supply"),
            "decimals": info.get("decimals"),
            "token_program": info.get("program"),
            "attempts": attempts,
        }

        if self.cfg.require_mint_authority_revoked and mint_auth:
            return done(False, f"mint authority activa ({mint_auth[:8]}...)", **details)
        if self.cfg.require_freeze_authority_revoked and freeze_auth:
            return done(False, f"freeze authority activa ({freeze_auth[:8]}...)", **details)

        return done(True, "autoritati revocate", **details)

    # ------------------------------------------------------------------
    async def _check_liquidity(self, mint: str, pipeline_deadline: float | None = None) -> StageResult:
        """b) Lichiditate din DexScreener, cu asteptare pentru indexare."""
        t0 = time.monotonic()
        deadline = t0 + max(0.0, self.cfg.liquidity_wait_seconds)
        # Nu depasim niciodata fereastra globala a pipeline-ului.
        if pipeline_deadline is not None:
            deadline = min(deadline, pipeline_deadline)
        url = DEXSCREENER_URL.format(mint=mint)
        attempts = 0
        last_error = ""
        # Detaliile ultimei perechi vazute; le pastram ca sa ajunga in verdictul
        # final chiar si cand iesim pe ramura "lichiditate necunoscuta".
        last_details: dict[str, Any] = {}

        def done(passed: bool, reason: str, **details: Any) -> StageResult:
            merged = {**last_details, **details}
            merged.setdefault("attempts", attempts)
            return StageResult(
                "liquidity", passed, reason, int((time.monotonic() - t0) * 1000), merged
            )

        while True:
            attempts += 1
            try:
                resp = await self.http.get(url)
            except httpx.HTTPError as exc:
                # httpx.HTTPError acopera si DecodingError / TooManyRedirects,
                # care NU sunt subclase de TransportError. Fara ele, o eroare de
                # decodare a raspunsului ar ocoli degradarea eleganta de mai jos.
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if resp.status_code == 200:
                    try:
                        body = resp.json()
                    except ValueError:
                        last_error = "raspuns non-JSON de la DexScreener"
                        body = None

                    if isinstance(body, dict):
                        pairs = body.get("pairs") or []
                        best = _best_pair(pairs)
                        if best is not None:
                            liq = _pair_liquidity_usd(best)
                            details = {
                                "liquidity_usd": liq,
                                "dex": best.get("dexId"),
                                "pair_address": best.get("pairAddress"),
                                "price_usd": _safe_float(best.get("priceUsd")),
                                "fdv": _safe_float(best.get("fdv"), default=None),
                                "volume_h24": _safe_float(
                                    (best.get("volume") or {}).get("h24"), default=None
                                ),
                            }
                            # --- Praguri de tractiune (fdv / volum) -------
                            # Pentru un token pe bonding curve lichiditatea
                            # lipseste, dar fdv si volumul exista si spun daca
                            # cineva chiar tranzactioneaza tokenul.
                            fdv = details["fdv"]
                            vol = details["volume_h24"]
                            praguri_active = (
                                self.cfg.min_volume_h24_usd > 0 or self.cfg.min_fdv_usd > 0
                            )

                            if praguri_active:
                                sub_prag = _sub_prag(
                                    fdv, vol,
                                    self.cfg.min_fdv_usd, self.cfg.min_volume_h24_usd,
                                )
                                if sub_prag is None:
                                    return done(
                                        True,
                                        f"tractiune reala: volum ${vol or 0:,.0f}"
                                        f", fdv ${fdv or 0:,.0f}",
                                        **details,
                                    )
                                # Sub prag NU inseamna respins pe loc: intrebarea e
                                # "atinge pragul in fereastra data?", nu "l-a atins
                                # in prima secunda in care DexScreener a raspuns".
                                # Continuam sa interogam pana expira fereastra.
                                last_details = details
                                last_error = sub_prag

                            elif liq is None:
                                details["liquidity_known"] = False
                                last_details = details
                                last_error = "DexScreener nu raporteaza lichiditate (bonding curve)"
                            else:
                                details["liquidity_known"] = True
                                if liq >= self.cfg.min_liquidity_usd:
                                    return done(True, f"lichiditate ${liq:,.0f}", **details)
                                return done(
                                    False,
                                    f"lichiditate ${liq:,.0f} < prag ${self.cfg.min_liquidity_usd:,.0f}",
                                    **details,
                                )
                        else:
                            last_error = "niciun pair indexat inca"
                elif resp.status_code == 429:
                    last_error = "DexScreener rate limit (429)"
                    await asyncio.sleep(2.0)
                else:
                    last_error = f"DexScreener HTTP {resp.status_code}"

            if time.monotonic() >= deadline:
                break
            # Nu depasim deadline-ul asteptand degeaba.
            sleep_for = min(self.cfg.liquidity_poll_interval, max(0.0, deadline - time.monotonic()))
            if sleep_for <= 0:
                break
            await asyncio.sleep(sleep_for)

        # Daca am cerut praguri de tractiune si nu au fost atinse, e un esec
        # ferm (stim cifra si e prea mica), nu un "nu stim".
        praguri_active = self.cfg.min_volume_h24_usd > 0 or self.cfg.min_fdv_usd > 0
        if praguri_active and last_details:
            return done(False, f"{last_error} (dupa {self.cfg.liquidity_wait_seconds:.0f}s)")

        if self.cfg.allow_missing_liquidity:
            return done(True, f"lichiditate necunoscuta, acceptat prin config ({last_error})", pending=True)
        return done(False, f"fara lichiditate detectabila ({last_error})", pending=True)

    # ------------------------------------------------------------------
    async def _check_rugcheck(self, mint: str, pipeline_deadline: float | None = None) -> StageResult:
        """c) A doua opinie: raportul sumar RugCheck.

        Daca `rugcheck_wait_seconds > 0`, reincercam cat timp serviciul spune ca
        nu are inca raport - altfel etapa nu contribuie deloc la token-urile de
        cateva secunde, exact cele pe care le vede botul.
        """
        t0 = time.monotonic()
        attempts = 0
        deadline = t0 + max(0.0, self.cfg.rugcheck_wait_seconds)
        if pipeline_deadline is not None:
            deadline = min(deadline, pipeline_deadline)

        while True:
            attempts += 1
            stage = await self._rugcheck_once(mint, t0, attempts)
            if not stage.details.get("no_report"):
                return stage
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return stage
            await asyncio.sleep(min(self.cfg.rugcheck_poll_interval, remaining))

    async def _rugcheck_once(self, mint: str, t0: float, attempts: int) -> StageResult:
        url = RUGCHECK_URL.format(mint=mint)

        def done(passed: bool, reason: str, **details: Any) -> StageResult:
            details.setdefault("attempts", attempts)
            return StageResult(
                "rugcheck", passed, reason, int((time.monotonic() - t0) * 1000), details
            )

        try:
            resp = await self.http.get(url)
        except httpx.HTTPError as exc:
            return self._rugcheck_soft_fail(done, f"RugCheck inaccesibil: {type(exc).__name__}")

        # RugCheck raspunde 400 {"error":"unable to generate report"} pentru
        # token-urile prea noi ca sa fi fost indexate - acelasi inteles ca 404,
        # nu o eroare de client. Il tratam ca "inca nu are raport", nu ca API
        # degradat, ca sa nu ascundem in loguri limita reala a serviciului.
        if resp.status_code in (400, 404):
            detail = ""
            try:
                body = resp.json()
                if isinstance(body, dict):
                    detail = str(body.get("error") or "")
            except ValueError:
                detail = ""
            return self._rugcheck_soft_fail(
                done,
                f"RugCheck nu are inca raport pentru acest mint"
                + (f" ({detail})" if detail else ""),
                no_report=True,
            )
        if resp.status_code == 429:
            return self._rugcheck_soft_fail(done, "RugCheck rate limit (429)")
        if resp.status_code != 200:
            return self._rugcheck_soft_fail(done, f"RugCheck HTTP {resp.status_code}")

        try:
            body = resp.json()
        except ValueError:
            return self._rugcheck_soft_fail(done, "RugCheck a returnat non-JSON")

        if not isinstance(body, dict):
            return self._rugcheck_soft_fail(done, "format neasteptat de la RugCheck")

        # API-ul a folosit in timp `score` si `score_normalised`; le acceptam pe ambele.
        score = _safe_float(body.get("score"), default=None)
        if score is None:
            score = _safe_float(body.get("score_normalised"), default=None)

        risks = body.get("risks") or []
        risk_names = [
            r.get("name") for r in risks if isinstance(r, dict) and r.get("name")
        ][:6]
        danger = [
            r.get("name")
            for r in risks
            if isinstance(r, dict) and str(r.get("level", "")).lower() == "danger"
        ]
        details = {
            "rugcheck_score": score,
            "risks": risk_names,
            "danger_count": len(danger),
        }

        if danger:
            return done(False, f"RugCheck: risc DANGER ({', '.join(danger[:2])})", **details)
        if score is None:
            return self._rugcheck_soft_fail(done, "RugCheck nu a returnat scor", **details)
        if score > self.cfg.max_rugcheck_score:
            return done(
                False,
                f"scor RugCheck {score:.0f} > prag {self.cfg.max_rugcheck_score:.0f}",
                **details,
            )
        return done(True, f"scor RugCheck {score:.0f}", **details)

    def _rugcheck_soft_fail(self, done, reason: str, **details: Any) -> StageResult:
        """RugCheck e backup: daca nu ne poate raspunde, decidem dupa `rugcheck_fail_open`.

        `no_report=True` inseamna ca serviciul functioneaza dar nu a apucat sa
        indexeze tokenul - o limita normala pentru lansari de secunde, nu o
        defectiune. Il marcam separat ca sa nu para ca API-ul e picat.
        """
        details.setdefault("no_report", False)
        details["degraded"] = not details["no_report"]
        if self.cfg.rugcheck_fail_open:
            return done(True, f"{reason} - trecut (fail-open)", **details)
        return done(False, f"{reason} - respins (fail-closed)", **details)


# ----------------------------------------------------------------------
def _expired(deadline: float | None) -> bool:
    """True daca am depasit fereastra in care verdictul mai are valoare."""
    return deadline is not None and time.monotonic() >= deadline


def _safe_float(value: Any, default: float | None = 0.0) -> Any:
    """Conversie tolerantă: DexScreener trimite si stringuri, si null."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _sub_prag(
    fdv: float | None, vol: float | None, min_fdv: float, min_vol: float
) -> str | None:
    """Motivul pentru care tokenul nu atinge pragurile de tractiune, sau None."""
    if min_vol > 0:
        if vol is None:
            return "DexScreener nu raporteaza inca volumul"
        if vol < min_vol:
            return f"volum 24h ${vol:,.0f} < prag ${min_vol:,.0f}"
    if min_fdv > 0:
        if fdv is None:
            return "DexScreener nu raporteaza inca fdv"
        if fdv < min_fdv:
            return f"fdv ${fdv:,.0f} < prag ${min_fdv:,.0f}"
    return None


def _pair_liquidity_usd(pair: dict[str, Any]) -> float | None:
    """Lichiditatea in USD a unei perechi, sau None daca DexScreener nu o raporteaza.

    Distinctia conteaza: absenta campului inseamna "nu stim" (token inca pe
    bonding curve), nu "zero".
    """
    liq = pair.get("liquidity")
    if not isinstance(liq, dict) or "usd" not in liq:
        return None
    return _safe_float(liq.get("usd"), default=None)


def _best_pair(pairs: Any) -> dict[str, Any] | None:
    """Perechea cu cea mai mare lichiditate cunoscuta (un token poate avea mai multe)."""
    if not isinstance(pairs, list):
        return None
    valid = [p for p in pairs if isinstance(p, dict)]
    if not valid:
        return None
    # Perechile fara lichiditate raportata coboara la finalul clasamentului, dar
    # raman candidate daca sunt singurele.
    return max(valid, key=lambda p: _pair_liquidity_usd(p) or -1.0)
