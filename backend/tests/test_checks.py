"""Teste pentru pipeline-ul anti-rug, cu RPC si HTTP simulate (fara retea)."""
from __future__ import annotations

import asyncio
import time
import unittest
from unittest import mock

import httpx

from bot.checks import AntiRugPipeline
from bot.config import BotConfig
from bot.rpc import RpcUnavailable

MINT = "5tMi4JLbfYvfhSCr1Bg8mbf2iCsMRHmYeMBoJVoWpump"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"


class FakeRpc:
    """Inlocuieste SolanaRpc: returneaza ce ii spunem, sau arunca.

    `missing_first` simuleaza cursa reala: contul de mint nu e inca propagat
    la primele N apeluri.
    """

    def __init__(self, value=None, error: Exception | None = None, missing_first: int = 0) -> None:
        self.value = value
        self.error = error
        self.missing_first = missing_first
        self.calls = 0
        self.commitments: list[str] = []
        self.deadlines: list = []

    async def get_account_info_parsed(
        self, address: str, commitment: str = "processed", *, deadline=None
    ):
        self.calls += 1
        self.commitments.append(commitment)
        self.deadlines.append(deadline)
        if self.error is not None:
            raise self.error
        if self.calls <= self.missing_first:
            return None
        return self.value


def mint_account(mint_auth=None, freeze_auth=None):
    return {
        "owner": TOKEN_PROGRAM,
        "data": {
            "parsed": {
                "type": "mint",
                "info": {
                    "mintAuthority": mint_auth,
                    "freezeAuthority": freeze_auth,
                    "supply": "1000000000",
                    "decimals": 6,
                    "isInitialized": True,
                },
            }
        },
    }


def http_with(routes: dict[str, tuple[int, dict | str]]) -> httpx.AsyncClient:
    """Client httpx care raspunde dupa host-ul apelat."""

    def handler(request: httpx.Request) -> httpx.Response:
        for key, (status, body) in routes.items():
            if key in str(request.url):
                if isinstance(body, str):
                    return httpx.Response(status, text=body)
                return httpx.Response(status, json=body)
        return httpx.Response(404, json={})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def cfg(**kwargs) -> BotConfig:
    base = dict(
        bot_id="test",
        check_liquidity=False,
        check_rugcheck=False,
        liquidity_wait_seconds=0,
        liquidity_poll_interval=0.01,
        authority_retry_delay=0.0,
    )
    base.update(kwargs)
    c = BotConfig(**base)  # type: ignore[arg-type]
    c.validate()
    return c


def run(coro):
    return asyncio.run(coro)


class TestAuthorityStage(unittest.TestCase):
    def test_pass_cand_autoritatile_sunt_revocate(self):
        async def go():
            http = http_with({})
            p = AntiRugPipeline(cfg(), FakeRpc(mint_account()), http)
            res = await p.run(MINT)
            await http.aclose()
            return res

        res = run(go())
        self.assertEqual(res.verdict, "PASS")
        self.assertIsNone(res.failed_stage)

    def test_fail_pe_mint_authority_activa(self):
        async def go():
            http = http_with({})
            p = AntiRugPipeline(cfg(), FakeRpc(mint_account(mint_auth="Abc123")), http)
            res = await p.run(MINT)
            await http.aclose()
            return res

        res = run(go())
        self.assertEqual(res.verdict, "FAIL")
        self.assertEqual(res.failed_stage, "authority")
        self.assertIn("mint authority", res.reason)

    def test_fail_pe_freeze_authority_activa(self):
        async def go():
            http = http_with({})
            p = AntiRugPipeline(cfg(), FakeRpc(mint_account(freeze_auth="Xyz789")), http)
            res = await p.run(MINT)
            await http.aclose()
            return res

        res = run(go())
        self.assertEqual(res.verdict, "FAIL")
        self.assertIn("freeze authority", res.reason)

    def test_rpc_picat_da_ERROR_nu_FAIL(self):
        async def go():
            http = http_with({})
            p = AntiRugPipeline(cfg(), FakeRpc(error=RpcUnavailable("timeout")), http)
            res = await p.run(MINT)
            await http.aclose()
            return res

        res = run(go())
        self.assertEqual(res.verdict, "ERROR")

    def test_fail_fast_nu_apeleaza_api_urile_externe(self):
        """Daca autoritatea pica, nu trebuie sa mai atingem DexScreener/RugCheck."""
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(200, json={"pairs": []})

        async def go():
            http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            p = AntiRugPipeline(
                cfg(check_liquidity=True, check_rugcheck=True),
                FakeRpc(mint_account(mint_auth="Abc123")),
                http,
            )
            res = await p.run(MINT)
            await http.aclose()
            return res

        res = run(go())
        self.assertEqual(res.failed_stage, "authority")
        self.assertEqual(calls, [], "pipeline-ul nu s-a oprit la primul esec")
        self.assertEqual(len(res.stages), 1)


class TestAuthorityRetries(unittest.TestCase):
    """Contul de mint apare cu intarziere; nu trebuie sa-l declaram inexistent prea repede."""

    def test_reincearca_pana_apare_contul(self):
        rpc = FakeRpc(mint_account(), missing_first=3)

        async def go():
            http = http_with({})
            p = AntiRugPipeline(cfg(authority_retries=5), rpc, http)
            res = await p.run(MINT)
            await http.aclose()
            return res

        res = run(go())
        self.assertEqual(res.verdict, "PASS")
        self.assertEqual(rpc.calls, 4)
        self.assertEqual(res.as_checks_dict()["authority"]["attempts"], 4)

    def test_renunta_dupa_toate_incercarile(self):
        rpc = FakeRpc(None, missing_first=99)

        async def go():
            http = http_with({})
            p = AntiRugPipeline(cfg(authority_retries=2), rpc, http)
            res = await p.run(MINT)
            await http.aclose()
            return res

        res = run(go())
        self.assertEqual(res.verdict, "ERROR")
        self.assertEqual(rpc.calls, 3)
        self.assertIn("nu exista on-chain", res.reason)

    def test_foloseste_commitment_ul_din_config(self):
        rpc = FakeRpc(mint_account())

        async def go():
            http = http_with({})
            p = AntiRugPipeline(cfg(authority_commitment="confirmed"), rpc, http)
            res = await p.run(MINT)
            await http.aclose()
            return res

        run(go())
        self.assertEqual(rpc.commitments, ["confirmed"])


class TestLiquidityStage(unittest.TestCase):
    def _pairs(self, liq_usd):
        return {"pairs": [{"liquidity": {"usd": liq_usd}, "dexId": "raydium",
                           "pairAddress": "P1", "priceUsd": "0.001", "fdv": 12345}]}

    def test_lichiditate_suficienta(self):
        async def go():
            http = http_with({"dexscreener": (200, self._pairs(5000))})
            p = AntiRugPipeline(cfg(check_liquidity=True, min_liquidity_usd=1000),
                                FakeRpc(mint_account()), http)
            res = await p.run(MINT)
            await http.aclose()
            return res

        res = run(go())
        self.assertEqual(res.verdict, "PASS")
        self.assertEqual(res.as_checks_dict()["liquidity"]["liquidity_usd"], 5000.0)

    def test_lichiditate_sub_prag(self):
        async def go():
            http = http_with({"dexscreener": (200, self._pairs(100))})
            p = AntiRugPipeline(cfg(check_liquidity=True, min_liquidity_usd=1000),
                                FakeRpc(mint_account()), http)
            res = await p.run(MINT)
            await http.aclose()
            return res

        res = run(go())
        self.assertEqual(res.verdict, "FAIL")
        self.assertEqual(res.failed_stage, "liquidity")

    def test_fara_pair_da_PENDING(self):
        async def go():
            http = http_with({"dexscreener": (200, {"pairs": []})})
            p = AntiRugPipeline(
                cfg(check_liquidity=True, allow_missing_liquidity=False),
                FakeRpc(mint_account()), http,
            )
            res = await p.run(MINT)
            await http.aclose()
            return res

        res = run(go())
        self.assertEqual(res.verdict, "PENDING")

    def test_allow_missing_liquidity_trece(self):
        async def go():
            http = http_with({"dexscreener": (200, {"pairs": []})})
            p = AntiRugPipeline(cfg(check_liquidity=True, allow_missing_liquidity=True),
                                FakeRpc(mint_account()), http)
            res = await p.run(MINT)
            await http.aclose()
            return res

        self.assertEqual(run(go()).verdict, "PASS")

    def test_alege_perechea_cu_lichiditate_maxima(self):
        body = {"pairs": [
            {"liquidity": {"usd": 100}, "dexId": "a"},
            {"liquidity": {"usd": 9000}, "dexId": "b"},
            {"liquidity": None, "dexId": "c"},
        ]}

        async def go():
            http = http_with({"dexscreener": (200, body)})
            p = AntiRugPipeline(cfg(check_liquidity=True, min_liquidity_usd=5000),
                                FakeRpc(mint_account()), http)
            res = await p.run(MINT)
            await http.aclose()
            return res

        res = run(go())
        self.assertEqual(res.verdict, "PASS")
        self.assertEqual(res.as_checks_dict()["liquidity"]["dex"], "b")

    def test_raspuns_stricat_nu_crapa(self):
        async def go():
            http = http_with({"dexscreener": (200, "<html>eroare</html>")})
            p = AntiRugPipeline(
                cfg(check_liquidity=True, allow_missing_liquidity=False),
                FakeRpc(mint_account()), http,
            )
            res = await p.run(MINT)
            await http.aclose()
            return res

        self.assertEqual(run(go()).verdict, "PENDING")


class TestLiquidityUnknownVsZero(unittest.TestCase):
    """DexScreener indexeaza perechea pump.fun imediat, dar NU raporteaza
    `liquidity` cat timp tokenul e pe bonding curve. Citirea ca $0 respingea
    orice lansare noua si raporta o cifra pe care nu o stim."""

    PAIR_FARA_LICHIDITATE = {"pairs": [{
        "dexId": "pumpfun", "pairAddress": "P1", "priceUsd": "0.0000024",
        "fdv": 2452.28, "volume": {"h24": 3633.89},
    }]}

    def test_lichiditate_absenta_nu_e_zero(self):
        async def go():
            http = http_with({"dexscreener": (200, self.PAIR_FARA_LICHIDITATE)})
            p = AntiRugPipeline(
                cfg(check_liquidity=True, min_liquidity_usd=1000,
                    allow_missing_liquidity=False),
                FakeRpc(mint_account()), http,
            )
            res = await p.run(MINT)
            await http.aclose()
            return res

        res = run(go())
        self.assertEqual(res.verdict, "PENDING")
        liq = res.as_checks_dict()["liquidity"]
        self.assertIsNone(liq["liquidity_usd"], "necunoscut trebuie sa fie None, nu 0")
        self.assertFalse(liq["liquidity_known"])
        self.assertNotIn("$0", res.reason)

    def test_lichiditate_absenta_trece_cu_allow_missing(self):
        async def go():
            http = http_with({"dexscreener": (200, self.PAIR_FARA_LICHIDITATE)})
            p = AntiRugPipeline(
                cfg(check_liquidity=True, min_liquidity_usd=1000,
                    allow_missing_liquidity=True),
                FakeRpc(mint_account()), http,
            )
            res = await p.run(MINT)
            await http.aclose()
            return res

        self.assertEqual(run(go()).verdict, "PASS")

    def test_zero_explicit_ramane_respins(self):
        """Daca DexScreener chiar raporteaza 0, e un fapt - respingem."""
        body = {"pairs": [{"dexId": "raydium", "liquidity": {"usd": 0}}]}

        async def go():
            http = http_with({"dexscreener": (200, body)})
            p = AntiRugPipeline(
                cfg(check_liquidity=True, min_liquidity_usd=1000,
                    allow_missing_liquidity=True),
                FakeRpc(mint_account()), http,
            )
            res = await p.run(MINT)
            await http.aclose()
            return res

        res = run(go())
        self.assertEqual(res.verdict, "FAIL")
        self.assertEqual(res.as_checks_dict()["liquidity"]["liquidity_usd"], 0.0)

    def test_alege_perechea_cu_lichiditate_cunoscuta(self):
        body = {"pairs": [
            {"dexId": "pumpfun"},                            # necunoscuta
            {"dexId": "raydium", "liquidity": {"usd": 8000}},  # cunoscuta
        ]}

        async def go():
            http = http_with({"dexscreener": (200, body)})
            p = AntiRugPipeline(
                cfg(check_liquidity=True, min_liquidity_usd=1000),
                FakeRpc(mint_account()), http,
            )
            res = await p.run(MINT)
            await http.aclose()
            return res

        res = run(go())
        self.assertEqual(res.verdict, "PASS")
        self.assertEqual(res.as_checks_dict()["liquidity"]["dex"], "raydium")


class TestPraguriDeTractiune(unittest.TestCase):
    """Pentru un token pe bonding curve, volumul si fdv sunt singurele semnale
    care separa o lansare reala de una moarta din start."""

    def _body(self, fdv=None, vol=None):
        pair = {"dexId": "pumpfun", "pairAddress": "P1", "priceUsd": "0.000002"}
        if fdv is not None:
            pair["fdv"] = fdv
        if vol is not None:
            pair["volume"] = {"h24": vol}
        return {"pairs": [pair]}

    def _ruleaza(self, body, **cfg_kw):
        async def go():
            http = http_with({"dexscreener": (200, body)})
            p = AntiRugPipeline(
                cfg(check_liquidity=True, allow_missing_liquidity=True, **cfg_kw),
                FakeRpc(mint_account()), http,
            )
            res = await p.run(MINT)
            await http.aclose()
            return res

        return run(go())

    def test_volum_prea_mic_respins(self):
        res = self._ruleaza(self._body(fdv=2900, vol=26), min_volume_h24_usd=5000)
        self.assertEqual(res.verdict, "FAIL")
        self.assertEqual(res.failed_stage, "liquidity")
        self.assertIn("volum 24h", res.reason)

    def test_volum_suficient_trece(self):
        res = self._ruleaza(self._body(fdv=145040, vol=39955), min_volume_h24_usd=5000)
        self.assertEqual(res.verdict, "PASS")
        self.assertEqual(res.as_checks_dict()["liquidity"]["volume_h24"], 39955.0)

    def test_fdv_prea_mic_respins(self):
        res = self._ruleaza(self._body(fdv=2556, vol=99999), min_fdv_usd=50000)
        self.assertEqual(res.verdict, "FAIL")
        self.assertIn("fdv", res.reason)

    def test_ambele_praguri_impreuna(self):
        res = self._ruleaza(self._body(fdv=145040, vol=39955),
                            min_volume_h24_usd=5000, min_fdv_usd=50000)
        self.assertEqual(res.verdict, "PASS")

    def test_volum_absent_nu_e_citit_ca_zero(self):
        """Daca DexScreener nu raporteaza inca volumul, nu inventam un zero."""
        res = self._ruleaza(self._body(fdv=2900), min_volume_h24_usd=5000)
        # allow_missing_liquidity=True -> trece, dar NU pe ramura de respingere
        self.assertNotIn("volum 24h", res.reason or "")

    def test_reincearca_pana_cand_tokenul_aduna_volum(self):
        """Intrebarea e "atinge pragul in fereastra?", nu "l-a atins imediat"."""
        apeluri = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            apeluri["n"] += 1
            vol = 120 if apeluri["n"] < 3 else 9200
            return httpx.Response(200, json={"pairs": [
                {"dexId": "pumpfun", "fdv": 3000, "volume": {"h24": vol}}
            ]})

        async def go():
            http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            p = AntiRugPipeline(
                cfg(check_liquidity=True, min_volume_h24_usd=5000,
                    liquidity_wait_seconds=5, liquidity_poll_interval=0.01),
                FakeRpc(mint_account()), http,
            )
            res = await p.run(MINT)
            await http.aclose()
            return res

        res = run(go())
        self.assertEqual(res.verdict, "PASS")
        self.assertEqual(apeluri["n"], 3, "trebuia sa mai interogheze de doua ori")
        # motivul e pe etapa, nu pe verdictul general
        self.assertIn("tractiune reala", res.as_checks_dict()["liquidity"]["reason"])

    def test_ramane_sub_prag_toata_fereastra_respins(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"pairs": [
                {"dexId": "pumpfun", "fdv": 2900, "volume": {"h24": 26}}
            ]})

        async def go():
            http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            p = AntiRugPipeline(
                cfg(check_liquidity=True, min_volume_h24_usd=5000,
                    liquidity_wait_seconds=0.05, liquidity_poll_interval=0.01),
                FakeRpc(mint_account()), http,
            )
            res = await p.run(MINT)
            await http.aclose()
            return res

        res = run(go())
        self.assertEqual(res.verdict, "FAIL")
        self.assertIn("volum 24h", res.reason)

    def test_fara_praguri_nimic_nu_se_schimba(self):
        res = self._ruleaza(self._body(fdv=1, vol=0))
        self.assertEqual(res.verdict, "PASS")


class TestRugcheckStage(unittest.TestCase):
    def test_scor_bun_trece(self):
        async def go():
            http = http_with({"rugcheck": (200, {"score": 500, "risks": []})})
            p = AntiRugPipeline(cfg(check_rugcheck=True, max_rugcheck_score=1000),
                                FakeRpc(mint_account()), http)
            res = await p.run(MINT)
            await http.aclose()
            return res

        self.assertEqual(run(go()).verdict, "PASS")

    def test_scor_prea_mare_pica(self):
        async def go():
            http = http_with({"rugcheck": (200, {"score": 5000, "risks": []})})
            p = AntiRugPipeline(cfg(check_rugcheck=True, max_rugcheck_score=1000),
                                FakeRpc(mint_account()), http)
            res = await p.run(MINT)
            await http.aclose()
            return res

        res = run(go())
        self.assertEqual(res.verdict, "FAIL")
        self.assertEqual(res.failed_stage, "rugcheck")

    def test_risc_danger_pica_indiferent_de_scor(self):
        body = {"score": 1, "risks": [{"name": "Mint Authority still enabled", "level": "danger"}]}

        async def go():
            http = http_with({"rugcheck": (200, body)})
            p = AntiRugPipeline(cfg(check_rugcheck=True), FakeRpc(mint_account()), http)
            res = await p.run(MINT)
            await http.aclose()
            return res

        res = run(go())
        self.assertEqual(res.verdict, "FAIL")
        self.assertIn("DANGER", res.reason)

    def test_fail_open_cand_api_ul_e_jos(self):
        async def go():
            http = http_with({"rugcheck": (503, {})})
            p = AntiRugPipeline(cfg(check_rugcheck=True, rugcheck_fail_open=True),
                                FakeRpc(mint_account()), http)
            res = await p.run(MINT)
            await http.aclose()
            return res

        self.assertEqual(run(go()).verdict, "PASS")

    def test_fail_closed_cand_api_ul_e_jos(self):
        async def go():
            http = http_with({"rugcheck": (503, {})})
            p = AntiRugPipeline(cfg(check_rugcheck=True, rugcheck_fail_open=False),
                                FakeRpc(mint_account()), http)
            res = await p.run(MINT)
            await http.aclose()
            return res

        res = run(go())
        self.assertEqual(res.verdict, "FAIL")
        self.assertIn("fail-closed", res.reason)

    def test_404_inseamna_raport_inexistent(self):
        async def go():
            http = http_with({"rugcheck": (404, {})})
            p = AntiRugPipeline(cfg(check_rugcheck=True, rugcheck_fail_open=True),
                                FakeRpc(mint_account()), http)
            res = await p.run(MINT)
            await http.aclose()
            return res

        res = run(go())
        self.assertEqual(res.verdict, "PASS")
        self.assertTrue(res.as_checks_dict()["rugcheck"]["no_report"])

    def test_400_unable_to_generate_report_nu_e_api_picat(self):
        """RugCheck raspunde 400 pentru token-uri prea noi. Nu e API degradat."""
        body = {"error": "unable to generate report"}

        async def go():
            http = http_with({"rugcheck": (400, body)})
            p = AntiRugPipeline(cfg(check_rugcheck=True, rugcheck_fail_open=True),
                                FakeRpc(mint_account()), http)
            res = await p.run(MINT)
            await http.aclose()
            return res

        res = run(go())
        self.assertEqual(res.verdict, "PASS")
        rc = res.as_checks_dict()["rugcheck"]
        self.assertTrue(rc["no_report"])
        self.assertFalse(rc["degraded"], "raport inexistent nu inseamna API picat")
        self.assertIn("nu are inca raport", rc["reason"])

    def test_reincearca_pana_apare_raportul(self):
        """Cu rugcheck_wait_seconds > 0, asteptam ca RugCheck sa indexeze tokenul."""
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] < 3:
                return httpx.Response(400, json={"error": "unable to generate report"})
            return httpx.Response(200, json={"score": 4000, "risks": []})

        async def go():
            http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            p = AntiRugPipeline(
                cfg(check_rugcheck=True, max_rugcheck_score=1000,
                    rugcheck_wait_seconds=5, rugcheck_poll_interval=0.01),
                FakeRpc(mint_account()), http,
            )
            res = await p.run(MINT)
            await http.aclose()
            return res

        res = run(go())
        # Raportul a aparut si a respins tokenul - exact ce nu se intampla
        # cand etapa renunta din prima.
        self.assertEqual(res.verdict, "FAIL")
        self.assertEqual(res.failed_stage, "rugcheck")
        self.assertEqual(calls["n"], 3)
        self.assertEqual(res.as_checks_dict()["rugcheck"]["attempts"], 3)

    def test_renunta_cand_expira_fereastra(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": "unable to generate report"})

        async def go():
            http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            p = AntiRugPipeline(
                cfg(check_rugcheck=True, rugcheck_fail_open=True,
                    rugcheck_wait_seconds=0.05, rugcheck_poll_interval=0.01),
                FakeRpc(mint_account()), http,
            )
            res = await p.run(MINT)
            await http.aclose()
            return res

        res = run(go())
        self.assertEqual(res.verdict, "PASS")
        self.assertTrue(res.as_checks_dict()["rugcheck"]["no_report"])

    def test_fara_asteptare_o_singura_incercare(self):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(400, json={"error": "unable to generate report"})

        async def go():
            http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            p = AntiRugPipeline(
                cfg(check_rugcheck=True, rugcheck_wait_seconds=0),
                FakeRpc(mint_account()), http,
            )
            res = await p.run(MINT)
            await http.aclose()
            return res

        run(go())
        self.assertEqual(calls["n"], 1)

    def test_500_ramane_api_degradat(self):
        async def go():
            http = http_with({"rugcheck": (500, {})})
            p = AntiRugPipeline(cfg(check_rugcheck=True, rugcheck_fail_open=True),
                                FakeRpc(mint_account()), http)
            res = await p.run(MINT)
            await http.aclose()
            return res

        rc = run(go()).as_checks_dict()["rugcheck"]
        self.assertTrue(rc["degraded"])
        self.assertFalse(rc["no_report"])


class TestDeadline(unittest.TestCase):
    """Un verdict tarziu nu foloseste nimanui: pipeline-ul trebuie sa se opreasca."""

    def test_deadline_depasit_opreste_inainte_de_orice_apel(self):
        rpc = FakeRpc(mint_account())

        async def go():
            http = http_with({})
            p = AntiRugPipeline(cfg(), rpc, http)
            res = await p.run(MINT, deadline=time.monotonic() - 1)
            await http.aclose()
            return res

        res = run(go())
        self.assertEqual(res.verdict, "ERROR")
        self.assertEqual(res.failed_stage, "deadline")
        self.assertEqual(rpc.calls, 0)

    def test_deadline_opreste_intre_etape(self):
        """Autoritatile apuca sa ruleze, dar timpul expira inainte de API-urile externe."""
        calls: list[str] = []
        expirations = iter([False, True, True, True])  # prima verificare trece, restul nu

        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(200, json={"pairs": []})

        rpc = FakeRpc(mint_account())

        async def go():
            http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            p = AntiRugPipeline(cfg(check_liquidity=True, check_rugcheck=True), rpc, http)
            with mock.patch("bot.checks._expired", side_effect=lambda _d: next(expirations, True)):
                res = await p.run(MINT, deadline=time.monotonic() + 60)
            await http.aclose()
            return res

        res = run(go())
        self.assertEqual(res.verdict, "ERROR")
        self.assertEqual(res.failed_stage, "deadline")
        self.assertEqual(rpc.calls, 1, "etapa on-chain trebuia sa ruleze")
        self.assertEqual(calls, [], "nu trebuia sa atingem DexScreener/RugCheck")

    def test_deadline_opreste_reincercarile_pe_mint(self):
        """Contul lipseste, dar nu mai reincercam daca fereastra a expirat."""
        rpc = FakeRpc(None, missing_first=99)

        async def go():
            http = http_with({})
            p = AntiRugPipeline(cfg(authority_retries=10), rpc, http)
            stage = await p._check_authorities(MINT, deadline=time.monotonic() - 1)
            await http.aclose()
            return stage

        stage = run(go())
        self.assertFalse(stage.passed)
        self.assertTrue(stage.details["inconclusive"])
        # O singura incercare: deadline-ul a taiat retry-urile.
        self.assertEqual(rpc.calls, 1)

    def test_fara_deadline_ruleaza_normal(self):
        async def go():
            http = http_with({})
            p = AntiRugPipeline(cfg(), FakeRpc(mint_account()), http)
            res = await p.run(MINT)
            await http.aclose()
            return res

        self.assertEqual(run(go()).verdict, "PASS")


class TestStageOrder(unittest.TestCase):
    def test_ordinea_etapelor_e_authority_liquidity_rugcheck(self):
        async def go():
            http = http_with({
                "dexscreener": (200, {"pairs": [{"liquidity": {"usd": 9999}}]}),
                "rugcheck": (200, {"score": 10, "risks": []}),
            })
            p = AntiRugPipeline(
                cfg(check_liquidity=True, check_rugcheck=True, min_liquidity_usd=100),
                FakeRpc(mint_account()), http,
            )
            res = await p.run(MINT)
            await http.aclose()
            return res

        res = run(go())
        self.assertEqual([s.stage for s in res.stages], ["authority", "liquidity", "rugcheck"])
        self.assertEqual(res.verdict, "PASS")


if __name__ == "__main__":
    unittest.main()
