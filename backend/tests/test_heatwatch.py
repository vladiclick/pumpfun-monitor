"""Teste pentru supravegherea comuna de "ia foc" (cereri grupate, fara retea)."""
from __future__ import annotations

import asyncio
import json
import unittest

import httpx

from bot.config import BotConfig
from bot.heatwatch import MAX_ADRESE_PE_CERERE, HeatWatcher, motiv_rece


def cfg(**kw) -> BotConfig:
    base = dict(
        bot_id="foc",
        heat_watch_seconds=5,
        heat_poll_interval=0.01,
        min_market_cap_usd=12000,
        max_market_cap_usd=400000,
        min_price_change_m5_pct=20,
        min_buy_sell_ratio_m5=1.4,
        min_volume_m5_usd=1500,
    )
    base.update(kw)
    c = BotConfig(**base)  # type: ignore[arg-type]
    c.validate()
    return c


def pereche(mint, mcap=20000, m5=534, buys=310, sells=186, vol=20000):
    return {
        "dexId": "pumpfun", "priceUsd": "0.0000201",
        "baseToken": {"address": mint, "symbol": "X"},
        "marketCap": mcap, "fdv": mcap,
        "priceChange": {"m5": m5, "h1": m5},
        "volume": {"m5": vol, "h24": vol * 3},
        "txns": {"m5": {"buys": buys, "sells": sells}},
    }


class TestMotivRece(unittest.TestCase):
    """Conditiile care fac un token "fierbinte", calibrate pe date reale:
    pe pump.fun capitalizarea de start e ~$2.600."""

    def _m(self, **kw):
        base = dict(market_cap=20000, price_change_m5=534, volume_m5=20000,
                    buys_m5=310, sells_m5=186)
        base.update(kw)
        return base

    def test_fierbinte(self):
        self.assertIsNone(motiv_rece(self._m(), cfg()))

    def test_capitalizare_de_start_respinsa(self):
        self.assertIn("capitalizare", motiv_rece(self._m(market_cap=2623), cfg()))

    def test_prea_tarziu(self):
        self.assertIn("prea tarziu", motiv_rece(self._m(market_cap=900000), cfg()))

    def test_pret_in_scadere(self):
        self.assertIn("in 5 min", motiv_rece(self._m(price_change_m5=-26), cfg()))

    def test_se_vinde_mai_mult(self):
        self.assertIn("vanzari", motiv_rece(self._m(buys_m5=22, sells_m5=24), cfg()))

    def test_volum_mic(self):
        self.assertIn("volum", motiv_rece(self._m(volume_m5=40), cfg()))

    def test_zero_vanzari_nu_imparte_la_zero(self):
        self.assertIsNone(motiv_rece(self._m(buys_m5=9, sells_m5=0), cfg()))

    def test_campuri_lipsa_nu_crapa(self):
        self.assertIsNotNone(motiv_rece({}, cfg()))


class TestActivitateSustinuta(unittest.TestCase):
    """Separa "urca si continua" de "a sarit o data si a inghetat"."""

    def _m(self, **kw):
        base = dict(market_cap=20000, price_change_m5=534, volume_m5=20000,
                    volume_h1=45000, buys_m5=310, sells_m5=186)
        base.update(kw)
        return base

    def _c(self, **kw):
        return cfg(min_buys_m5=25, min_volume_h1_usd=20000, **kw)

    def test_activitate_reala_trece(self):
        self.assertIsNone(motiv_rece(self._m(), self._c()))

    def test_doua_tranzactii_nu_inseamna_interes(self):
        """3 cumparari / 0 vanzari trece orice raport, dar nu inseamna nimic."""
        rece = motiv_rece(self._m(buys_m5=3, sells_m5=0), self._c())
        self.assertIsNotNone(rece)
        self.assertIn("cumparari in 5 min", rece)

    def test_varf_de_5_minute_fara_sustinere_respins(self):
        """Volum mare acum, dar nimic pe ora: exact tokenul care ingheata."""
        rece = motiv_rece(self._m(volume_h1=900), self._c())
        self.assertIn("volum pe ora", rece)

    def test_praguri_oprite_nu_schimba_nimic(self):
        # aceleasi date, dar cu pragurile noi pe 0: raportul ramane valid
        self.assertIsNone(motiv_rece(self._m(buys_m5=3, sells_m5=1, volume_h1=10), cfg()))


class TestHeatWatcher(unittest.IsolatedAsyncioTestCase):
    def _watcher(self, handler, **cfg_kw):
        aprinse = []
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        w = HeatWatcher(cfg(**cfg_kw), http, lambda u, m: aprinse.append((u.mint, m)))
        return w, http, aprinse

    async def test_grupeaza_cererile_cate_30(self):
        """Motivul intregii rescrieri: o cerere pentru 30 de token-uri, nu 30."""
        cereri = []

        def handler(request: httpx.Request) -> httpx.Response:
            cereri.append(str(request.url))
            return httpx.Response(200, json={"pairs": []})

        w, http, _ = self._watcher(handler)
        for i in range(70):
            w.watch(f"mint{i:03d}", {})
        await w._un_ciclu()
        await http.aclose()

        self.assertEqual(len(cereri), 3, "70 de token-uri = 3 cereri, nu 70")
        adrese = cereri[0].rsplit("/", 1)[-1].split(",")
        self.assertLessEqual(len(adrese), MAX_ADRESE_PE_CERERE)

    async def test_anunta_cand_ia_foc_si_scoate_din_lista(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"pairs": [pereche("MINT_A")]})

        w, http, aprinse = self._watcher(handler)
        w.watch("MINT_A", {"symbol": "AAA"})
        await w._un_ciclu()
        await http.aclose()

        self.assertEqual(len(aprinse), 1)
        mint, masura = aprinse[0]
        self.assertEqual(mint, "MINT_A")
        self.assertEqual(masura["market_cap"], 20000)
        self.assertEqual(masura["buys_m5"], 310)
        self.assertEqual(w.stats["watching"], 0, "trebuia scos dupa alerta")
        self.assertEqual(w.stats["hot"], 1)

    async def test_tokenul_rece_ramane_sub_observatie(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"pairs": [pereche("MINT_B", mcap=2623, m5=0.4)]})

        w, http, aprinse = self._watcher(handler)
        w.watch("MINT_B", {})
        await w._un_ciclu()
        await http.aclose()

        self.assertEqual(aprinse, [])
        self.assertEqual(w.stats["watching"], 1)

    async def test_se_aprinde_la_a_treia_citire(self):
        """Exact scopul supravegherii: prinde momentul in care porneste."""
        n = {"i": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            n["i"] += 1
            p = pereche("MINT_C") if n["i"] >= 3 else pereche("MINT_C", mcap=2623, m5=0.4, vol=8)
            return httpx.Response(200, json={"pairs": [p]})

        w, http, aprinse = self._watcher(handler)
        w.watch("MINT_C", {})
        for _ in range(3):
            await w._un_ciclu()
        await http.aclose()

        self.assertEqual(len(aprinse), 1)
        self.assertEqual(aprinse[0][1]["reads"], 3)

    async def test_expira_dupa_fereastra(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"pairs": []})

        w, http, aprinse = self._watcher(handler, heat_watch_seconds=0.02)
        w.watch("MINT_D", {})
        await asyncio.sleep(0.05)
        await w._un_ciclu()
        await http.aclose()

        self.assertEqual(w.stats["watching"], 0)
        self.assertEqual(w.stats["expired"], 1)
        self.assertEqual(aprinse, [])

    async def test_alege_perechea_cu_capitalizarea_cea_mai_mare(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"pairs": [
                pereche("MINT_E", mcap=13000),
                pereche("MINT_E", mcap=41000),
            ]})

        w, http, aprinse = self._watcher(handler)
        w.watch("MINT_E", {})
        await w._un_ciclu()
        await http.aclose()

        self.assertEqual(aprinse[0][1]["market_cap"], 41000)

    async def test_erorile_de_retea_nu_opresc_supravegherea(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("retea picata", request=request)

        w, http, aprinse = self._watcher(handler)
        w.watch("MINT_F", {})
        await w._un_ciclu()
        await http.aclose()

        self.assertEqual(w.stats["watching"], 1, "tokenul ramane, nu-l pierdem")
        self.assertGreaterEqual(w.stats["errors"], 1)

    async def test_429_nu_pierde_tokenul(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, json={})

        w, http, _ = self._watcher(handler)
        w.watch("MINT_G", {})
        await w._un_ciclu()
        await http.aclose()
        self.assertEqual(w.stats["watching"], 1)

    async def test_handler_care_arunca_nu_opreste_ciclul(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"pairs": [pereche("MINT_H")]})

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        def explodeaza(u, m):
            raise RuntimeError("bum")

        w = HeatWatcher(cfg(), http, explodeaza)
        w.watch("MINT_H", {})
        await w._un_ciclu()   # nu trebuie sa arunce
        await http.aclose()
        self.assertEqual(w.stats["hot"], 1)

    async def test_dezactivat_nu_urmareste_nimic(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("nu trebuia sa se faca nicio cerere")

        w, http, _ = self._watcher(handler, heat_watch_seconds=0)
        self.assertFalse(w.enabled)
        w.watch("MINT_I", {})
        w.start()
        await w.stop()
        await http.aclose()
        self.assertEqual(w.stats["watching"], 0)

    async def test_plafon_scoate_cele_mai_vechi(self):
        """Numarul de cereri creste liniar cu cate token-uri urmarim."""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"pairs": []})

        w, http, _ = self._watcher(handler, heat_max_watched=5)
        for i in range(9):
            w.watch(f"m{i}", {})
        await http.aclose()

        self.assertEqual(w.stats["watching"], 5)
        self.assertEqual(w.stats["evicted"], 4)
        self.assertNotIn("m0", w._urmarite, "cel mai vechi trebuia scos primul")
        self.assertIn("m8", w._urmarite)

    async def test_acelasi_mint_nu_e_adaugat_de_doua_ori(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"pairs": []})

        w, http, _ = self._watcher(handler)
        w.watch("MINT_J", {})
        w.watch("MINT_J", {})
        await http.aclose()
        self.assertEqual(w.stats["watching"], 1)


if __name__ == "__main__":
    unittest.main()
