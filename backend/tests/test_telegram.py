"""Teste pentru notificatorul Telegram (bot/telegram.py), cu transport HTTP simulat."""
from __future__ import annotations

import asyncio
import json
import time
import unittest
from unittest import mock

import httpx

from bot.telegram import (
    TELEGRAM_API,
    TelegramError,
    TelegramNotifier,
    discover_chat_id,
    format_pass_message,
)

MINT = "5tMi4JLbfYvfhSCr1Bg8mbf2iCsMRHmYeMBoJVoWpump"
TOKEN = "tok123"
CHAT_ID = "123456789"

# Referinta catre clasa reala, capturata inainte de orice mock.patch - vezi
# patched_client() mai jos, de ce nu putem folosi direct httpx.AsyncClient acolo.
_RealAsyncClient = httpx.AsyncClient


def notifier_with(handler, *, token: str = TOKEN, chat_id: str = CHAT_ID, **kw) -> TelegramNotifier:
    """TelegramNotifier cu clientul HTTP inlocuit de un transport simulat (fara retea)."""
    n = TelegramNotifier(token, chat_id, **kw)
    n._client = httpx.AsyncClient(  # type: ignore[attr-defined]
        transport=httpx.MockTransport(handler)
    )
    # Backoff mic: cele 3 incercari trebuie sa incapa in fereastra testului.
    n._backoff_base = 0.02
    return n


def patched_client(handler):
    """Inlocuieste httpx.AsyncClient folosit intern de discover_chat_id cu unul simulat.

    Atentie: bot.telegram.httpx e acelasi obiect modul ca httpx importat aici, deci
    a patch-ui .AsyncClient patch-uieste global atributul. Folosim _RealAsyncClient
    (capturat la incarcarea fisierului) in loc de httpx.AsyncClient in factory, ca
    sa nu ne apelam propriul mock la infinit.
    """
    return mock.patch(
        "bot.telegram.httpx.AsyncClient",
        side_effect=lambda *a, **kw: _RealAsyncClient(transport=httpx.MockTransport(handler)),
    )


# --- A. Feature dezactivata curat cand nu e configurata -------------------


class TestFeatureDezactivata(unittest.IsolatedAsyncioTestCase):
    """Fara token sau chat_id, notificatorul trebuie sa fie complet inofensiv."""

    def test_token_gol_dezactiveaza(self):
        n = TelegramNotifier("", CHAT_ID)
        self.assertFalse(n.enabled)

    def test_chat_id_gol_dezactiveaza(self):
        n = TelegramNotifier(TOKEN, "")
        self.assertFalse(n.enabled)

    def test_ambele_setate_activeaza(self):
        n = TelegramNotifier(TOKEN, CHAT_ID)
        self.assertTrue(n.enabled)

    async def test_dezactivat_start_notify_stop_nu_arunca_si_nu_atinge_reteaua(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("notificatorul dezactivat nu trebuia sa faca niciun apel HTTP")

        n = TelegramNotifier("", "")
        n._client = httpx.AsyncClient(  # type: ignore[attr-defined]
            transport=httpx.MockTransport(handler)
        )

        n.start()
        n.notify_pass(bot_id="b", name="X", symbol="X", mint=MINT, latency_ms=100)
        await n.stop()

        self.assertEqual(n.stats, {"sent": 0, "failed": 0, "dropped": 0, "queued": 0})


# --- B. Trimite doar pe PASS ------------------------------------------------


class TestTrimitePass(unittest.IsolatedAsyncioTestCase):
    """notify_pass trebuie sa trimita exact un mesaj sendMessage catre Telegram."""

    async def test_trimite_un_singur_mesaj_cu_corpul_corect(self):
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"ok": True, "result": {}})

        n = notifier_with(handler)
        n.start()
        n.notify_pass(bot_id="bot-1", name="Runner Coin", symbol="RUN", mint=MINT, latency_ms=123)
        await asyncio.sleep(0.3)
        await n.stop()

        self.assertEqual(len(requests), 1)
        req = requests[0]
        self.assertTrue(str(req.url).startswith(TELEGRAM_API))
        self.assertIn(f"/bot{TOKEN}/sendMessage", str(req.url))

        body = json.loads(req.content)
        self.assertEqual(str(body["chat_id"]), CHAT_ID)
        self.assertEqual(body["parse_mode"], "HTML")
        self.assertIs(body["disable_web_page_preview"], True)
        self.assertIn("RUN", body["text"])
        self.assertIn(MINT, body["text"])
        self.assertIn(f"https://pump.fun/coin/{MINT}", body["text"])
        self.assertEqual(n.stats["sent"], 1)

    def test_nu_exista_metoda_de_notificare_pe_fail(self):
        """Contractul "doar PASS" e impus structural: nu exista notify_fail/notify_verdict."""
        n = TelegramNotifier(TOKEN, CHAT_ID)
        self.assertFalse(hasattr(n, "notify_fail"))
        self.assertFalse(hasattr(n, "notify_verdict"))


# --- C. Esecul Telegram nu strica nimic ------------------------------------


class TestRezistentaLaEsecuri(unittest.IsolatedAsyncioTestCase):
    """O eroare Telegram nu are voie sa doboare botul sau task-ul notificatorului."""

    async def test_eroare_de_retea_nu_omoara_task_ul(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("retea picata", request=request)

        n = notifier_with(handler)
        n.start()
        n.notify_pass(bot_id="b", name="X", symbol="X", mint=MINT, latency_ms=1)
        await asyncio.sleep(1.8)

        self.assertFalse(n._task.done(), "task-ul notificatorului a murit")
        self.assertGreaterEqual(n.stats["failed"], 1)
        await n.stop()

    async def test_400_ok_false_nu_e_reincercat_la_infinit(self):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(400, json={"ok": False, "description": "chat not found"})

        n = notifier_with(handler)
        n.start()
        n.notify_pass(bot_id="b", name="X", symbol="X", mint=MINT, latency_ms=1)
        await asyncio.sleep(1.8)

        self.assertEqual(n.stats["failed"], 1)
        self.assertLess(calls["n"], 5, "un refuz definitiv nu trebuie reincercat la infinit")
        self.assertFalse(n._task.done())
        await n.stop()

    async def test_500_apoi_200_livreaza_dupa_reincercare(self):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(500)
            return httpx.Response(200, json={"ok": True, "result": {}})

        n = notifier_with(handler)
        n.start()
        n.notify_pass(bot_id="b", name="X", symbol="X", mint=MINT, latency_ms=1)
        await asyncio.sleep(1.8)

        self.assertEqual(n.stats["sent"], 1)
        self.assertGreaterEqual(calls["n"], 2)
        self.assertFalse(n._task.done())
        await n.stop()

    async def test_exceptie_arbitrara_nu_omoara_task_ul(self):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("ceva complet neasteptat")
            return httpx.Response(200, json={"ok": True, "result": {}})

        n = notifier_with(handler)
        n.start()
        n.notify_pass(bot_id="b", name="X", symbol="X", mint=MINT, latency_ms=1)
        await asyncio.sleep(1.8)

        self.assertFalse(n._task.done(), "task-ul notificatorului a murit")
        self.assertGreaterEqual(calls["n"], 2, "nu a mai incercat dupa eroare")
        await n.stop()


# --- D. Fire-and-forget chiar nu blocheaza ----------------------------------


class TestNuBlocheaza(unittest.IsolatedAsyncioTestCase):
    """notify_pass e sincron si nu trebuie sa astepte raspunsul retelei."""

    async def test_notify_pass_nu_asteapta_raspunsul_lent(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(0.5)
            return httpx.Response(200, json={"ok": True, "result": {}})

        n = notifier_with(handler)
        n.start()

        start = time.monotonic()
        n.notify_pass(bot_id="b", name="X", symbol="X", mint=MINT, latency_ms=1)
        elapsed = time.monotonic() - start

        self.assertLess(elapsed, 0.1, "notify_pass e sincron si nu trebuie sa blocheze")
        await n.stop(drain_timeout=0.1)

    async def test_coada_plina_arunca_cele_mai_vechi(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": True, "result": {}})

        n = notifier_with(handler, queue_size=3)
        for i in range(10):
            n.notify_pass(bot_id="b", name="X", symbol=f"S{i}", mint=MINT, latency_ms=i)

        self.assertEqual(n.stats["queued"], 3)
        self.assertEqual(n.stats["dropped"], 7)
        await n._client.aclose()


# --- E. format_pass_message -------------------------------------------------


class TestFormatPassMessage(unittest.TestCase):
    """format_pass_message construieste textul HTML trimis la Telegram."""

    def test_contine_toate_campurile_relevante(self):
        msg = format_pass_message(bot_id="bot-7", name="Runner Coin", symbol="RUN",
                                   mint=MINT, latency_ms=321)
        self.assertIn("RUN", msg)
        self.assertIn("Runner Coin", msg)
        self.assertIn(MINT, msg)
        self.assertIn(f"https://pump.fun/coin/{MINT}", msg)
        self.assertIn("bot-7", msg)
        self.assertIn("321", msg)

    def test_contine_link_phantom(self):
        """Linkul Phantom deschide direct aplicatia pe telefon."""
        text = format_pass_message(
            bot_id="rapid", name="Test", symbol="TST",
            mint="DTdwF5TCisjxPMWTBUCwEDrfGHy3jMTc9SV6JJV1piwK", latency_ms=100,
        )
        # Domeniul trebuie sa fie phantom.com: doar el are fisierele care
        # leaga linkul de aplicatia mobila (trade.phantom.com da 404 acolo).
        self.assertIn(
            "https://phantom.com/tokens/solana/DTdwF5TCisjxPMWTBUCwEDrfGHy3jMTc9SV6JJV1piwK",
            text,
        )
        self.assertNotIn("trade.phantom.com", text)
        # pump.fun ramane ca a doua optiune
        self.assertIn("https://pump.fun/coin/", text)

    def test_arata_de_ce_e_fierbinte(self):
        """Mesajul trebuie sa spuna motivul, nu doar numele tokenului."""
        text = format_pass_message(
            bot_id="elita", name="Kikutaro", symbol="KIKU",
            mint="DTdwF5TCisjxPMWTBUCwEDrfGHy3jMTc9SV6JJV1piwK", latency_ms=241000,
            heat={"market_cap": 20087, "price_change_m5": 534,
                  "buys_m5": 310, "sells_m5": 186, "volume_m5": 20737},
        )
        self.assertIn("$20,087", text)
        self.assertIn("+534%", text)
        self.assertIn("310 cumparari / 186 vanzari", text)

    def test_fara_date_de_foc_mesajul_ramane_valid(self):
        text = format_pass_message(
            bot_id="rapid", name="X", symbol="X", mint="M", latency_ms=1, heat=None,
        )
        self.assertIn("$X", text)

    def test_html_periculos_este_scapat(self):
        msg = format_pass_message(bot_id="b", name="<script>alert(1)</script> & Co",
                                   symbol="AB", mint=MINT, latency_ms=1)
        self.assertNotIn("<script>", msg)
        self.assertIn("&amp;", msg)

    def test_latency_none_nu_crapa(self):
        msg = format_pass_message(bot_id="b", name="X", symbol="X", mint=MINT, latency_ms=None)
        self.assertIn(MINT, msg)


# --- F. discover_chat_id -----------------------------------------------------


class TestDiscoverChatId(unittest.IsolatedAsyncioTestCase):
    """discover_chat_id citeste getUpdates si extrage chat-urile posibile."""

    async def test_returneaza_chat_urile_cele_mai_noi_primele(self):
        body = {
            "ok": True,
            "result": [
                {
                    "update_id": 1,
                    "message": {
                        "message_id": 1, "date": 1000,
                        "chat": {"id": 111, "type": "private", "first_name": "Alice"},
                        "from": {"id": 111, "first_name": "Alice", "username": "alice"},
                        "text": "/start",
                    },
                },
                {
                    "update_id": 2,
                    "message": {
                        "message_id": 2, "date": 2000,
                        "chat": {"id": -222, "type": "group", "title": "Grupul Meu"},
                        "from": {"id": 333, "first_name": "Bob", "username": "bob"},
                        "text": "salut",
                    },
                },
            ],
        }

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=body)

        with patched_client(handler):
            result = await discover_chat_id(TOKEN)

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["chat_id"], -222)
        self.assertEqual(result[0]["type"], "group")
        self.assertEqual(result[0]["title"], "Grupul Meu")
        self.assertEqual(result[1]["chat_id"], 111)

    async def test_chat_duplicat_este_deduplicat(self):
        body = {
            "ok": True,
            "result": [
                {"update_id": 1, "message": {
                    "message_id": 1, "date": 1000,
                    "chat": {"id": 111, "type": "private", "first_name": "Alice"},
                    "from": {"id": 111, "first_name": "Alice"}, "text": "a",
                }},
                {"update_id": 2, "message": {
                    "message_id": 2, "date": 2000,
                    "chat": {"id": 111, "type": "private", "first_name": "Alice"},
                    "from": {"id": 111, "first_name": "Alice"}, "text": "b",
                }},
            ],
        }

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=body)

        with patched_client(handler):
            result = await discover_chat_id(TOKEN)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["chat_id"], 111)

    async def test_refuz_api_arunca_telegram_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})

        with patched_client(handler):
            with self.assertRaises(TelegramError) as ctx:
                await discover_chat_id("tok-gresit")

        self.assertIn("Unauthorized", str(ctx.exception))

    async def test_eroare_de_retea_arunca_telegram_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("retea picata", request=request)

        with patched_client(handler):
            with self.assertRaises(TelegramError):
                await discover_chat_id(TOKEN)

    async def test_actualizari_stricate_sunt_ignorate_fara_eroare(self):
        body = {
            "ok": True,
            "result": [
                {"update_id": 1},  # fara "message"
                {"update_id": 2, "message": {"date": 1, "chat": "nu-e-dict", "text": "x"}},
                {"update_id": 3, "message": {"date": 1, "chat": {"type": "private"}, "text": "x"}},
                {"update_id": 4, "message": {
                    "date": 1, "chat": {"id": 999, "type": "private", "first_name": "Ok"},
                    "from": {"id": 999, "first_name": "Ok"}, "text": "buna",
                }},
            ],
        }

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=body)

        with patched_client(handler):
            result = await discover_chat_id(TOKEN)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["chat_id"], 999)

    async def test_lista_de_actualizari_absenta_nu_crapa(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": True, "result": "nu-e-lista"})

        with patched_client(handler):
            result = await discover_chat_id(TOKEN)

        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
