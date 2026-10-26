"""Teste pentru orchestrator, emitent si managerul de procese."""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import httpx

from app.manager import BotManager
from bot.config import BotConfig
from bot.emitter import EventEmitter
from bot.events import Event
from bot.listener import Detection
from bot.runner import MAX_DETECTION_AGE_S, BotRunner
from bot.settings import PUMPFUN_PROGRAM_ID
from tests.test_parser import _create_event_log, MINT


def cfg(**kw) -> BotConfig:
    base = dict(bot_id="runner-test", check_liquidity=False, check_rugcheck=False,
                authority_retry_delay=0.0)
    base.update(kw)
    c = BotConfig(**base)  # type: ignore[arg-type]
    c.validate()
    return c


class CollectingEmitter:
    """Inlocuieste EventEmitter: retine evenimentele in loc sa le trimita."""

    def __init__(self) -> None:
        self.events: list[Event] = []
        self.stats = {"sent": 0, "dropped": 0, "queued": 0}

    def start(self) -> None:
        pass

    async def stop(self, drain_timeout: float = 3.0) -> None:
        pass

    def emit(self, event: Event) -> None:
        self.events.append(event)

    def by_type(self, kind: str) -> list[Event]:
        return [e for e in self.events if e.type == kind]


def make_runner(account_value=None, **cfg_kw) -> BotRunner:
    runner = BotRunner(cfg(**cfg_kw))
    runner.emitter = CollectingEmitter()  # type: ignore[assignment]

    async def fake_account(address, commitment="processed", *, deadline=None):
        return account_value

    runner.rpc.get_account_info_parsed = fake_account  # type: ignore[assignment]
    runner.pipeline.rpc = runner.rpc
    return runner


def mint_account(mint_auth=None, freeze_auth=None):
    return {
        "owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        "data": {"parsed": {"type": "mint", "info": {
            "mintAuthority": mint_auth, "freezeAuthority": freeze_auth,
            "supply": "1000", "decimals": 6, "isInitialized": True,
        }}},
    }


def detection(age: float = 0.0) -> Detection:
    logs = [
        f"Program {PUMPFUN_PROGRAM_ID} invoke [1]",
        "Program log: Instruction: CreateV2",
        _create_event_log(name="Runner Coin", symbol="RUN"),
        f"Program {PUMPFUN_PROGRAM_ID} success",
    ]
    return Detection(signature="s" * 88, logs=logs, detected_at=time.monotonic() - age)


class TestRunnerProcess(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        for runner in getattr(self, "_runners", []):
            await runner.rpc.aclose()
            await runner.http.aclose()

    def track(self, runner):
        self._runners = getattr(self, "_runners", [])
        self._runners.append(runner)
        return runner

    async def test_calea_rapida_nu_atinge_get_transaction(self):
        runner = self.track(make_runner(mint_account()))
        called = False

        async def boom(sig):
            nonlocal called
            called = True
            return None

        runner.rpc.get_transaction = boom  # type: ignore[assignment]
        await runner._process(detection())

        self.assertFalse(called, "s-a apelat getTransaction desi tokenul era in loguri")
        verdicts = runner.emitter.by_type("verdict")
        self.assertEqual(len(verdicts), 1)
        self.assertEqual(verdicts[0].verdict, "PASS")
        self.assertEqual(verdicts[0].symbol, "RUN")
        self.assertEqual(verdicts[0].mint, MINT)
        self.assertEqual(runner.stats.rpc_fallbacks, 0)

    async def test_latenta_este_masurata_de_la_detectie(self):
        runner = self.track(make_runner(mint_account()))
        await runner._process(detection(age=0.5))
        latency = runner.emitter.by_type("verdict")[0].latency_ms
        self.assertGreaterEqual(latency, 500)

    async def test_autoritate_activa_da_fail(self):
        runner = self.track(make_runner(mint_account(mint_auth="Abc")))
        await runner._process(detection())
        v = runner.emitter.by_type("verdict")[0]
        self.assertEqual(v.verdict, "FAIL")
        self.assertEqual(v.failed_stage, "authority")
        self.assertEqual(runner.stats.failed, 1)

    async def test_detectie_veche_este_aruncata(self):
        runner = self.track(make_runner(mint_account()))
        await runner._process(detection(age=MAX_DETECTION_AGE_S + 5))
        v = runner.emitter.by_type("verdict")[0]
        self.assertEqual(v.verdict, "ERROR")
        self.assertEqual(v.failed_stage, "queue")
        self.assertEqual(runner.stats.dropped, 1)

    async def test_fallback_pe_get_transaction_cand_logurile_nu_ajung(self):
        runner = self.track(make_runner(mint_account()))
        calls = []

        async def fake_tx(sig):
            calls.append(sig)
            return {
                "meta": {"err": None, "logMessages": [_create_event_log(symbol="FB")],
                         "innerInstructions": []},
                "transaction": {"message": {"accountKeys": [], "instructions": []}},
            }

        runner.rpc.get_transaction = fake_tx  # type: ignore[assignment]
        det = Detection(signature="z" * 88, logs=["fara Program data"], detected_at=time.monotonic())
        await runner._process(det)

        self.assertEqual(len(calls), 1)
        self.assertEqual(runner.stats.rpc_fallbacks, 1)
        self.assertEqual(runner.emitter.by_type("verdict")[0].symbol, "FB")

    async def test_token_neparsabil_da_error(self):
        runner = self.track(make_runner(mint_account()))

        async def no_tx(sig):
            return None

        runner.rpc.get_transaction = no_tx  # type: ignore[assignment]
        det = Detection(signature="q" * 88, logs=[], detected_at=time.monotonic())
        await runner._process(det)

        v = runner.emitter.by_type("verdict")[0]
        self.assertEqual(v.verdict, "ERROR")
        self.assertEqual(v.failed_stage, "parse")

    async def test_pipeline_care_crapa_nu_doboara_botul(self):
        runner = self.track(make_runner(mint_account()))

        async def explode(mint):
            raise RuntimeError("bum")

        runner.pipeline.run = explode  # type: ignore[assignment]
        await runner._process(detection())

        v = runner.emitter.by_type("verdict")[0]
        self.assertEqual(v.verdict, "ERROR")
        self.assertEqual(v.failed_stage, "pipeline")
        self.assertEqual(runner.stats.errors, 1)


class TestEmitter(unittest.IsolatedAsyncioTestCase):
    def _emitter(self, handler, **kw) -> EventEmitter:
        em = EventEmitter("http://backend.test", "tok", **kw)
        em._client = httpx.AsyncClient(  # type: ignore[attr-defined]
            transport=httpx.MockTransport(handler),
            headers={"x-ingest-token": "tok"},
        )
        return em

    async def test_trimite_evenimentele_in_loturi(self):
        received: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            received.extend(json.loads(request.content)["events"])
            return httpx.Response(200, json={"ok": True})

        em = self._emitter(handler)
        em.start()
        for i in range(5):
            em.emit(Event(bot_id="b", type="stats", data={"i": i}))
        await asyncio.sleep(0.3)
        await em.stop()

        self.assertEqual(len(received), 5)
        self.assertEqual(em.stats["sent"], 5)

    async def test_retrimite_dupa_esec_temporar(self):
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] == 1:
                return httpx.Response(503)
            return httpx.Response(200, json={"ok": True})

        em = self._emitter(handler)
        em.start()
        em.emit(Event(bot_id="b", type="stats"))
        await asyncio.sleep(1.6)
        await em.stop()

        self.assertGreaterEqual(attempts["n"], 2)
        self.assertEqual(em.stats["sent"], 1)

    async def test_401_nu_intra_in_bucla_infinita(self):
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            return httpx.Response(401)

        em = self._emitter(handler)
        em.start()
        em.emit(Event(bot_id="b", type="stats"))
        await asyncio.sleep(0.4)
        await em.stop()

        self.assertEqual(attempts["n"], 1)
        self.assertEqual(em.stats["dropped"], 1)

    async def test_coada_plina_arunca_cele_mai_vechi(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": True})

        em = self._emitter(handler, queue_size=3)
        for i in range(10):
            em.emit(Event(bot_id="b", type="stats", data={"i": i}))
        self.assertEqual(em.stats["queued"], 3)
        self.assertEqual(em.stats["dropped"], 7)
        await em._client.aclose()


class TelegramSpion:
    """Inlocuieste TelegramNotifier: retine apelurile in loc sa trimita."""

    def __init__(self, enabled: bool = True, exploda: bool = False) -> None:
        self._enabled = enabled
        self.exploda = exploda
        self.apeluri: list[dict] = []
        self.stats = {"sent": 0, "failed": 0, "dropped": 0, "queued": 0}

    @property
    def enabled(self) -> bool:
        return self._enabled

    def start(self) -> None:
        pass

    async def stop(self, drain_timeout: float = 3.0) -> None:
        pass

    def notify_pass(self, **kwargs) -> None:
        if self.exploda:
            raise RuntimeError("Telegram a crapat")
        self.apeluri.append(kwargs)


class TestCablareTelegram(unittest.IsolatedAsyncioTestCase):
    """Notificarea trebuie sa plece DOAR pe PASS si sa nu poata opri pipeline-ul."""

    async def asyncTearDown(self):
        for runner in getattr(self, "_runners", []):
            await runner.rpc.aclose()
            await runner.http.aclose()

    def _runner(self, account, spion, **cfg_kw):
        r = make_runner(account, **cfg_kw)
        r.telegram = spion
        self._runners = getattr(self, "_runners", [])
        self._runners.append(r)
        return r

    async def test_trimite_pe_pass(self):
        spion = TelegramSpion()
        r = self._runner(mint_account(), spion)
        await r._process(detection())

        self.assertEqual(len(spion.apeluri), 1)
        apel = spion.apeluri[0]
        self.assertEqual(apel["symbol"], "RUN")
        self.assertEqual(apel["mint"], MINT)
        self.assertEqual(apel["bot_id"], "runner-test")
        self.assertIsInstance(apel["latency_ms"], int)

    async def test_telegram_notify_false_nu_trimite(self):
        """Instantele zgomotoase alimenteaza dashboardul, dar nu si telefonul."""
        spion = TelegramSpion()
        r = self._runner(mint_account(), spion, telegram_notify=False)
        await r._process(detection())

        self.assertEqual(r.emitter.by_type("verdict")[0].verdict, "PASS")
        self.assertEqual(spion.apeluri, [])

    async def test_nu_trimite_pe_fail(self):
        spion = TelegramSpion()
        r = self._runner(mint_account(mint_auth="Abc"), spion)
        await r._process(detection())

        self.assertEqual(r.emitter.by_type("verdict")[0].verdict, "FAIL")
        self.assertEqual(spion.apeluri, [], "nu trebuie sa notifice un token respins")

    async def test_nu_trimite_pe_error(self):
        spion = TelegramSpion()
        r = self._runner(None, spion, authority_retries=0)

        async def no_tx(sig):
            return None

        r.rpc.get_transaction = no_tx  # type: ignore[assignment]
        await r._process(detection())

        self.assertEqual(r.emitter.by_type("verdict")[0].verdict, "ERROR")
        self.assertEqual(spion.apeluri, [])

    async def test_esecul_telegram_nu_opreste_verdictul(self):
        """Chiar daca notificarea arunca, verdictul trebuie emis normal."""
        spion = TelegramSpion(exploda=True)
        r = self._runner(mint_account(), spion)
        await r._process(detection())

        verdicte = r.emitter.by_type("verdict")
        self.assertEqual(len(verdicte), 1)
        self.assertEqual(verdicte[0].verdict, "PASS")

    async def test_notificatorul_real_dezactivat_nu_arunca(self):
        """Fara token, obiectul real e inert - fara mock-uri."""
        from bot.telegram import TelegramNotifier

        r = make_runner(mint_account())
        r.telegram = TelegramNotifier("", "")
        self._runners = getattr(self, "_runners", [])
        self._runners.append(r)

        self.assertFalse(r.telegram.enabled)
        r.telegram.start()
        await r._process(detection())
        await r.telegram.stop()

        self.assertEqual(r.emitter.by_type("verdict")[0].verdict, "PASS")


class TestEmitterRezistent(unittest.IsolatedAsyncioTestCase):
    """Task-ul emitentului nu are voie sa moara: daca moare, botul ruleaza mai
    departe dar nu mai raporteaza nimic, tacut."""

    def _emitter(self, handler) -> EventEmitter:
        em = EventEmitter("http://backend.test", "tok")
        em._client = httpx.AsyncClient(  # type: ignore[attr-defined]
            transport=httpx.MockTransport(handler), headers={"x-ingest-token": "tok"}
        )
        return em

    async def test_supravietuieste_unei_erori_httpx_neasteptate(self):
        """DecodingError NU e subclasa de TransportError - inainte omora task-ul."""
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.DecodingError("Content-Encoding stricat", request=request)
            return httpx.Response(200, json={"ok": True})

        em = self._emitter(handler)
        em.start()
        em.emit(Event(bot_id="b", type="stats"))
        await asyncio.sleep(1.6)

        self.assertFalse(em._task.done(), "task-ul emitentului a murit")
        self.assertEqual(em.stats["sent"], 1, "evenimentul nu a fost retrimis")
        await em.stop()

    async def test_supravietuieste_unei_exceptii_arbitrare(self):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("ceva complet neasteptat")
            return httpx.Response(200, json={"ok": True})

        em = self._emitter(handler)
        em.start()
        em.emit(Event(bot_id="b", type="stats"))
        await asyncio.sleep(2.0)

        self.assertFalse(em._task.done(), "task-ul emitentului a murit")
        self.assertGreaterEqual(calls["n"], 2, "nu a mai incercat dupa eroare")
        await em.stop()

    async def test_3xx_nu_e_considerat_succes(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"location": "/altundeva"})

        em = self._emitter(handler)
        em.start()
        em.emit(Event(bot_id="b", type="stats"))
        await asyncio.sleep(0.6)

        self.assertEqual(em.stats["sent"], 0)
        self.assertEqual(em.stats["dropped"], 1)
        await em.stop()


class TestManager(unittest.IsolatedAsyncioTestCase):
    """Managerul chiar porneste procese - folosim un config real, dar cu bot oprit rapid."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.manager = BotManager()

    async def asyncTearDown(self):
        await self.manager.shutdown_all()
        self._tmp.cleanup()

    async def test_start_stop_proces_real(self):
        cfg_path = self.dir / "probe.json"
        BotConfig.from_dict({"bot_id": "probe", "check_liquidity": False,
                             "check_rugcheck": False}).save(cfg_path)

        with mock.patch("app.manager.config_path_for", lambda b: self.dir / f"{b}.json"):
            await self.manager.upsert_config({"bot_id": "probe"})
            inst = await self.manager.start("probe")
            self.assertEqual(inst.status, "running")
            self.assertIsNotNone(inst.pid)

            inst = await self.manager.stop("probe")
            self.assertEqual(inst.status, "stopped")
            self.assertFalse(inst.autorestart)

    async def test_actiuni_pe_bot_inexistent(self):
        with self.assertRaises(KeyError):
            await self.manager.start("fantoma")
        with self.assertRaises(KeyError):
            await self.manager.stop("fantoma")
        self.assertFalse(await self.manager.delete("fantoma"))

    async def test_start_dublu_nu_creeaza_al_doilea_proces(self):
        with mock.patch("app.manager.config_path_for", lambda b: self.dir / f"{b}.json"):
            await self.manager.upsert_config({"bot_id": "probe2"})
            a = await self.manager.start("probe2")
            b = await self.manager.start("probe2")
            self.assertEqual(a.pid, b.pid)
            await self.manager.stop("probe2")

    async def test_reciteste_configul_de_pe_disc_la_pornire(self):
        """Editezi fisierul, apesi Restart -> trebuie sa se aplice."""
        with mock.patch("app.manager.config_path_for", lambda b: self.dir / f"{b}.json"):
            await self.manager.upsert_config({"bot_id": "reload", "min_liquidity_usd": 1000})

            # simulam editarea manuala a fisierului
            cale = self.dir / "reload.json"
            date = json.loads(cale.read_text())
            date["min_liquidity_usd"] = 7777
            cale.write_text(json.dumps(date))

            inst = await self.manager.start("reload")
            self.assertEqual(inst.cfg.min_liquidity_usd, 7777)
            await self.manager.stop("reload")

    async def test_config_invalid_este_respins(self):
        from bot.config import ConfigError

        with self.assertRaises(ConfigError):
            await self.manager.upsert_config({"bot_id": "NU VALID"})


if __name__ == "__main__":
    unittest.main()
