"""Teste pentru API-ul FastAPI (ingest, REST, WebSocket) - fara retea externa."""
from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

import app.main as main
from app.db import Database
from bot.events import Event
from bot.settings import SETTINGS

TOKEN = SETTINGS.ingest_token
AUTH = {"x-ingest-token": TOKEN}


class ApiTestBase(unittest.TestCase):
    """Reconfigureaza serverul sa foloseasca o baza de date temporara."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        main.db = Database(Path(self._tmp.name) / "api.db")
        # Nu vrem sa pornim procese reale de bot in teste.
        self._patch = mock.patch.object(main.manager, "load_from_disk", lambda: None)
        self._patch.start()
        self.client = TestClient(main.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self._patch.stop()
        self._tmp.cleanup()

    def ev(self, **kw):
        base = Event(bot_id="b1", type="verdict", verdict="PASS",
                     mint="MintAAA", symbol="AAA", latency_ms=150).to_dict()
        base.update(kw)
        return base


class TestIngest(ApiTestBase):
    def test_ingest_valid(self):
        r = self.client.post("/api/ingest", json={"events": [self.ev()]}, headers=AUTH)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["stored"], 1)

    def test_token_lipsa_da_401(self):
        r = self.client.post("/api/ingest", json={"events": [self.ev()]})
        self.assertEqual(r.status_code, 401)

    def test_token_gresit_da_401(self):
        r = self.client.post("/api/ingest", json={"events": [self.ev()]},
                             headers={"x-ingest-token": "gresit"})
        self.assertEqual(r.status_code, 401)

    def test_payload_invalid_da_422(self):
        cases = [
            {"events": []},                                   # lot gol
            {"events": [{"bot_id": "b", "type": "x"}]},        # lipsesc event_id/ts
            {"events": [self.ev(ts=1700)]},                    # ts in secunde, nu ms
        ]
        for body in cases:
            self.assertEqual(self.client.post("/api/ingest", json=body, headers=AUTH).status_code, 422)

    def test_campuri_extra_sunt_acceptate(self):
        r = self.client.post("/api/ingest", json={"events": [self.ev(camp_nou="valoare")]}, headers=AUTH)
        self.assertEqual(r.status_code, 200)
        stored = self.client.get("/api/events").json()["events"]
        self.assertEqual(stored[0]["camp_nou"], "valoare")


class TestEventsApi(ApiTestBase):
    def test_istoric_si_filtre(self):
        self.client.post("/api/ingest", headers=AUTH, json={"events": [
            self.ev(verdict="PASS", symbol="AAA"),
            self.ev(verdict="FAIL", symbol="BBB"),
            self.ev(bot_id="b2", verdict="PASS", symbol="CCC"),
        ]})
        self.assertEqual(self.client.get("/api/events").json()["count"], 3)
        self.assertEqual(self.client.get("/api/events?verdict=FAIL").json()["count"], 1)
        self.assertEqual(self.client.get("/api/events?bot_id=b2").json()["count"], 1)
        self.assertEqual(self.client.get("/api/events?search=CC").json()["count"], 1)

    def test_limita_invalida_da_422(self):
        self.assertEqual(self.client.get("/api/events?limit=0").status_code, 422)
        self.assertEqual(self.client.get("/api/events?limit=99999").status_code, 422)

    def test_stats(self):
        self.client.post("/api/ingest", headers=AUTH, json={"events": [
            self.ev(latency_ms=100), self.ev(verdict="FAIL", latency_ms=300),
        ]})
        stats = self.client.get("/api/stats").json()["stats"]
        self.assertEqual(stats["passed"], 1)
        self.assertEqual(stats["avg_latency_ms"], 200)


class TestInstancesApi(ApiTestBase):
    def test_creare_si_listare(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("bot.config.CONFIG_DIR", Path(tmp)), \
                 mock.patch("app.manager.config_path_for", lambda b: Path(tmp) / f"{b}.json"):
                r = self.client.post("/api/instances", json={
                    "bot_id": "test-bot", "label": "Test", "min_liquidity_usd": 2500,
                })
                self.assertEqual(r.status_code, 200, r.text)
                ids = [i["bot_id"] for i in self.client.get("/api/instances").json()["instances"]]
                self.assertIn("test-bot", ids)

    def test_bot_id_invalid_da_422(self):
        r = self.client.post("/api/instances", json={"bot_id": "NU E VALID"})
        self.assertEqual(r.status_code, 422)

    def test_actiuni_pe_bot_inexistent_dau_404(self):
        for path in ("start", "stop", "restart"):
            self.assertEqual(self.client.post(f"/api/instances/fantoma/{path}").status_code, 404)
        self.assertEqual(self.client.delete("/api/instances/fantoma").status_code, 404)


class TestWebSocket(ApiTestBase):
    def test_snapshot_la_conectare(self):
        with self.client.websocket_connect("/ws") as ws:
            msg = ws.receive_json()
            self.assertEqual(msg["type"], "snapshot")
            self.assertIn("instances", msg)
            self.assertIn("server", msg)

    def test_ping_pong(self):
        with self.client.websocket_connect("/ws") as ws:
            ws.receive_json()  # snapshot
            ws.send_text("ping")
            self.assertEqual(ws.receive_json()["type"], "pong")

    def test_evenimentele_ajung_live(self):
        with self.client.websocket_connect("/ws") as ws:
            ws.receive_json()  # snapshot
            self.client.post("/api/ingest", headers=AUTH,
                             json={"events": [self.ev(symbol="LIVE")]})
            msg = ws.receive_json()
            self.assertEqual(msg["type"], "events")
            self.assertEqual(msg["events"][0]["symbol"], "LIVE")


class TestLimitaMarimeEveniment(ApiTestBase):
    def test_eveniment_urias_respins(self):
        r = self.client.post(
            "/api/ingest", headers=AUTH,
            json={"events": [self.ev(data={"balast": "x" * 200_000})]},
        )
        self.assertEqual(r.status_code, 422)

    def test_eveniment_normal_acceptat(self):
        r = self.client.post(
            "/api/ingest", headers=AUTH,
            json={"events": [self.ev(checks={"authority": {"passed": True}})]},
        )
        self.assertEqual(r.status_code, 200)


class TestTokenDashboard(ApiTestBase):
    """Cand DASHBOARD_TOKEN e setat, REST-ul si WebSocket-ul il cer."""

    def setUp(self):
        super().setUp()
        # Settings e un dataclass frozen: inlocuim tot obiectul, nu un camp.
        self._patch_token = mock.patch.object(
            main, "SETTINGS", replace(main.SETTINGS, dashboard_token="secret-dash")
        )
        self._patch_token.start()

    def tearDown(self):
        self._patch_token.stop()
        super().tearDown()

    def test_rest_fara_token_da_401(self):
        for path in ("/api/instances", "/api/events", "/api/stats"):
            self.assertEqual(self.client.get(path).status_code, 401, path)

    def test_control_fara_token_da_401(self):
        self.assertEqual(self.client.post("/api/instances/oricare/stop").status_code, 401)
        self.assertEqual(self.client.delete("/api/instances/oricare").status_code, 401)

    def test_token_prin_header(self):
        r = self.client.get("/api/instances", headers={"x-dashboard-token": "secret-dash"})
        self.assertEqual(r.status_code, 200)

    def test_token_prin_query(self):
        self.assertEqual(self.client.get("/api/events?token=secret-dash").status_code, 200)

    def test_token_gresit(self):
        self.assertEqual(self.client.get("/api/events?token=aiurea").status_code, 401)

    def test_health_ramane_public(self):
        self.assertEqual(self.client.get("/api/health").status_code, 200)

    def test_websocket_fara_token_e_refuzat(self):
        from starlette.websockets import WebSocketDisconnect

        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect("/ws") as ws:
                ws.receive_json()

    def test_websocket_cu_token_merge(self):
        with self.client.websocket_connect("/ws?token=secret-dash") as ws:
            self.assertEqual(ws.receive_json()["type"], "snapshot")


class TestStatisticiInPushuri(ApiTestBase):
    """Panoul de instante afisa 0 dupa start/stop: push-urile WS nu aveau `stats`."""

    def test_snapshot_contine_stats(self):
        self.client.post("/api/ingest", headers=AUTH, json={"events": [self.ev()]})
        with self.client.websocket_connect("/ws") as ws:
            snap = ws.receive_json()
            self.assertEqual(snap["type"], "snapshot")
            for inst in snap["instances"]:
                self.assertIn("stats", inst)

    def test_push_dupa_actiune_contine_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("bot.config.CONFIG_DIR", Path(tmp)), \
                 mock.patch("app.manager.config_path_for", lambda b: Path(tmp) / f"{b}.json"):
                with self.client.websocket_connect("/ws") as ws:
                    ws.receive_json()  # snapshot
                    self.client.post("/api/instances", json={"bot_id": "push-test"})
                    for _ in range(4):
                        msg = ws.receive_json()
                        if msg.get("type") == "instances":
                            self.assertTrue(msg["instances"])
                            for inst in msg["instances"]:
                                self.assertIn("stats", inst)
                            return
                    self.fail("nu a sosit niciun push de tip 'instances'")


class TestHealth(ApiTestBase):
    def test_health(self):
        body = self.client.get("/api/health").json()
        self.assertTrue(body["ok"])
        self.assertIn("uptime_s", body)


if __name__ == "__main__":
    unittest.main()
