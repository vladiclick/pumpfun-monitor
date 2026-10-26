"""Teste pentru listener, config, emitter si baza de date."""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from app.db import Database
from bot.config import BotConfig, ConfigError
from bot.settings import mask_endpoint
from bot.events import Event
from bot.listener import PumpFunListener, SeenCache, logs_contain_pumpfun_create
from bot.settings import PUMPFUN_PROGRAM_ID

SIG = "5" * 88
ATA = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"


def pump_logs(instruction: str = "CreateV2", extra=()):
    """Urma de loguri a unei invocari pump.fun, ca in mainnet."""
    return [
        f"Program {PUMPFUN_PROGRAM_ID} invoke [1]",
        f"Program log: Instruction: {instruction}",
        *extra,
        f"Program {PUMPFUN_PROGRAM_ID} success",
    ]


def _notification(logs, signature=SIG, err=None):
    return json.dumps({
        "jsonrpc": "2.0",
        "method": "logsNotification",
        "params": {"result": {"value": {"signature": signature, "err": err, "logs": logs}}},
    })


def _listener() -> PumpFunListener:
    cfg = BotConfig(bot_id="test")
    cfg.validate()
    return PumpFunListener(cfg, "wss://exemplu")


class TestSeenCache(unittest.TestCase):
    def test_deduplica(self):
        c = SeenCache(maxsize=3)
        self.assertTrue(c.add_if_new("a"))
        self.assertFalse(c.add_if_new("a"))

    def test_evictie_lru(self):
        c = SeenCache(maxsize=2)
        c.add_if_new("a")
        c.add_if_new("b")
        c.add_if_new("c")           # scoate "a"
        self.assertEqual(len(c), 2)
        self.assertTrue(c.add_if_new("a"))


class TestLogScanner(unittest.TestCase):
    """Detectia trebuie sa fie exacta: doar `Create`/`CreateV2` ale pump.fun."""

    def test_create_v2(self):
        self.assertTrue(logs_contain_pumpfun_create(pump_logs("CreateV2")))

    def test_create_legacy(self):
        self.assertTrue(logs_contain_pumpfun_create(pump_logs("Create")))

    def test_ignora_instructiuni_cu_prefix_asemanator(self):
        for name in ("CreateFeeSharingConfig", "CreateTokenAccountWithSeed", "CreateIdempotent"):
            self.assertFalse(logs_contain_pumpfun_create(pump_logs(name)), name)

    def test_ignora_create_din_alt_program(self):
        logs = [
            f"Program {ATA} invoke [1]",
            "Program log: Instruction: Create",
            f"Program {ATA} success",
        ]
        self.assertFalse(logs_contain_pumpfun_create(logs))

    def test_ignora_create_al_ata_ului_imbricat_in_pumpfun(self):
        """ATA-ul invocat din pump.fun logheaza tot 'Create' - nu trebuie sa conteze."""
        logs = [
            f"Program {PUMPFUN_PROGRAM_ID} invoke [1]",
            "Program log: Instruction: Buy",
            f"Program {ATA} invoke [2]",
            "Program log: Instruction: Create",
            f"Program {ATA} success",
            f"Program {PUMPFUN_PROGRAM_ID} success",
        ]
        self.assertFalse(logs_contain_pumpfun_create(logs))

    def test_ignora_buy(self):
        self.assertFalse(logs_contain_pumpfun_create(pump_logs("Buy")))

    def test_loguri_gunoi(self):
        self.assertFalse(logs_contain_pumpfun_create([None, 123, "", "Program X"]))

    def test_loguri_trunchiate_nu_crapa(self):
        logs = [f"Program {PUMPFUN_PROGRAM_ID} success", "Program log: Instruction: CreateV2"]
        self.assertFalse(logs_contain_pumpfun_create(logs))


class TestListenerExtraction(unittest.TestCase):
    def test_detecteaza_create(self):
        det = _listener()._extract(_notification(pump_logs()))
        self.assertIsNotNone(det)
        self.assertEqual(det.signature, SIG)
        # Logurile trebuie pastrate: din ele scoatem tokenul fara apel RPC.
        self.assertTrue(det.logs)
        self.assertGreater(det.detected_at, 0)

    def test_ignora_alte_instructiuni(self):
        self.assertIsNone(_listener()._extract(_notification(pump_logs("Buy"))))

    def test_ignora_tranzactii_esuate(self):
        raw = _notification(pump_logs(), err={"InstructionError": []})
        self.assertIsNone(_listener()._extract(raw))

    def test_ignora_duplicate(self):
        lst = _listener()
        raw = _notification(pump_logs())
        self.assertIsNotNone(lst._extract(raw))
        self.assertIsNone(lst._extract(raw))

    def test_json_stricat_nu_crapa(self):
        lst = _listener()
        for bad in ("{nu e json", "", "[]", '{"method":"altceva"}', b"\xff\xfe"):
            self.assertIsNone(lst._extract(bad))

    def test_semnatura_prea_scurta_e_respinsa(self):
        self.assertIsNone(_listener()._extract(_notification(pump_logs(), signature="scurt")))

    def test_confirmare_subscribe_e_ignorata(self):
        raw = json.dumps({"jsonrpc": "2.0", "result": 1, "id": 1})
        self.assertIsNone(_listener()._extract(raw))


class TestBotConfig(unittest.TestCase):
    def test_bot_id_invalid(self):
        for bad in ("", "A", "cu spatiu", "../evadare", "x" * 40, "Majuscule"):
            with self.assertRaises(ConfigError):
                BotConfig.from_dict({"bot_id": bad})

    def test_praguri_invalide(self):
        with self.assertRaises(ConfigError):
            BotConfig.from_dict({"bot_id": "ok", "min_liquidity_usd": -5})
        with self.assertRaises(ConfigError):
            BotConfig.from_dict({"bot_id": "ok", "max_concurrent_checks": 0})
        with self.assertRaises(ConfigError):
            BotConfig.from_dict({"bot_id": "ok", "ws_backoff_initial": 10, "ws_backoff_max": 1})

    def test_chei_necunoscute_sunt_ignorate(self):
        cfg = BotConfig.from_dict({"bot_id": "ok", "camp_inexistent": 42})
        self.assertEqual(cfg.bot_id, "ok")
        self.assertIn("chei ignorate", cfg.notes)

    def test_label_implicit_este_bot_id(self):
        self.assertEqual(BotConfig.from_dict({"bot_id": "ok"}).label, "ok")

    def test_salvare_si_incarcare(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.json"
            BotConfig.from_dict({"bot_id": "salvat", "min_liquidity_usd": 777}).save(path)
            self.assertAlmostEqual(BotConfig.load(path).min_liquidity_usd, 777)

    def test_json_stricat_da_eroare_clara(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rupt.json"
            path.write_text("{nu e json valid", encoding="utf-8")
            with self.assertRaises(ConfigError) as ctx:
                BotConfig.load(path)
            self.assertIn("JSON invalid", str(ctx.exception))

    def test_fisier_inexistent(self):
        with self.assertRaises(ConfigError):
            BotConfig.load(Path("/cale/care/nu/exista.json"))


class TestMascareSecrete(unittest.TestCase):
    """Cheia unui provider RPC nu are voie sa ajunga in dashboard sau in SQLite."""

    def test_mascheaza_toate_formele_de_cheie(self):
        cazuri = [
            ("https://mainnet.helius-rpc.com/?api-key=SECRET123", "SECRET123"),
            ("https://solana-mainnet.g.alchemy.com/v2/CHEIA_MEA", "CHEIA_MEA"),
            ("https://x.solana-mainnet.quiknode.pro/abc123def/", "abc123def"),
            ("wss://rpc.ankr.com/solana/ws/TOKENUL", "TOKENUL"),
            ("http://user:parola@host:8899/x", "parola"),
        ]
        for url, secret in cazuri:
            masked = mask_endpoint(url)
            self.assertNotIn(secret, masked, f"secret scapat pentru {url}")
            self.assertTrue(masked.startswith(("http://", "https://", "wss://", "ws://")))

    def test_pastreaza_endpoint_ul_public(self):
        self.assertEqual(
            mask_endpoint("https://api.mainnet-beta.solana.com"),
            "https://api.mainnet-beta.solana.com",
        )

    def test_input_invalid(self):
        self.assertEqual(mask_endpoint(""), "")
        self.assertEqual(mask_endpoint("nu e url"), "***")

    def test_to_dict_mascheaza_implicit(self):
        cfg = BotConfig.from_dict({
            "bot_id": "cu-cheie",
            "rpc_http": "https://mainnet.helius-rpc.com/?api-key=SECRET123",
            "rpc_ws": "wss://mainnet.helius-rpc.com/?api-key=SECRET123",
        })
        payload = cfg.to_dict()
        self.assertNotIn("SECRET123", str(payload))
        # Doar scrierea pe disc are voie sa vada valoarea reala.
        self.assertIn("SECRET123", str(cfg.to_dict(reveal_secrets=True)))

    def test_fisierul_salvat_pastreaza_valoarea_reala(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.json"
            BotConfig.from_dict({
                "bot_id": "cu-cheie",
                "rpc_http": "https://mainnet.helius-rpc.com/?api-key=SECRET123",
            }).save(path)
            self.assertIn("SECRET123", path.read_text(encoding="utf-8"))
            self.assertEqual(
                BotConfig.load(path).rpc_http,
                "https://mainnet.helius-rpc.com/?api-key=SECRET123",
            )


class TestEvent(unittest.TestCase):
    def test_elimina_campurile_goale(self):
        d = Event(bot_id="b", type="stats").to_dict()
        self.assertNotIn("mint", d)
        self.assertIn("event_id", d)
        self.assertGreater(d["ts"], 1_600_000_000_000)

    def test_pastreaza_latenta_zero(self):
        # 0 e o valoare valida si nu trebuie sters ca "gol".
        d = Event(bot_id="b", type="verdict", latency_ms=0).to_dict()
        self.assertEqual(d["latency_ms"], 0)


class TestDatabase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "t.db")
        await self.db.connect()

    async def asyncTearDown(self):
        await self.db.close()
        self._tmp.cleanup()

    def _ev(self, **kw):
        base = Event(bot_id="b1", type="verdict", verdict="PASS", latency_ms=120,
                     mint="MintAAA", symbol="AAA", name="Alpha").to_dict()
        base.update(kw)
        return base

    async def test_insert_si_query(self):
        await self.db.insert_events([self._ev(), self._ev(verdict="FAIL", latency_ms=300)])
        rows = await self.db.query_events(limit=10)
        self.assertEqual(len(rows), 2)

    async def test_duplicatele_sunt_ignorate(self):
        ev = self._ev()
        await self.db.insert_events([ev])
        stored = await self.db.insert_events([ev])
        self.assertEqual(stored, 0)
        self.assertEqual(len(await self.db.query_events()), 1)

    async def test_filtrare_dupa_verdict_si_bot(self):
        await self.db.insert_events([
            self._ev(verdict="PASS"),
            self._ev(verdict="FAIL"),
            self._ev(bot_id="b2", verdict="PASS"),
        ])
        self.assertEqual(len(await self.db.query_events(verdict="pass")), 2)
        self.assertEqual(len(await self.db.query_events(bot_id="b2")), 1)

    async def test_cautare_text(self):
        await self.db.insert_events([self._ev(symbol="DOGE"), self._ev(symbol="PEPE")])
        self.assertEqual(len(await self.db.query_events(search="DOG")), 1)

    async def test_agregate(self):
        await self.db.insert_events([
            self._ev(verdict="PASS", latency_ms=100),
            self._ev(verdict="FAIL", latency_ms=300),
            Event(bot_id="b1", type="token_detected").to_dict(),
        ])
        stats = await self.db.bot_stats("b1")
        self.assertEqual(stats["passed"], 1)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["detected"], 1)
        self.assertEqual(stats["avg_latency_ms"], 200)

    async def test_lot_gol(self):
        self.assertEqual(await self.db.insert_events([]), 0)

    async def test_limita_este_plafonata(self):
        await self.db.insert_events([self._ev() for _ in range(5)])
        self.assertLessEqual(len(await self.db.query_events(limit=10_000)), 5)


if __name__ == "__main__":
    unittest.main()
