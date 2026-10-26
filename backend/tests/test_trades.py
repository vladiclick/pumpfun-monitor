"""Teste pentru citirea tranzactiilor din loguri (fara retea)."""
from __future__ import annotations

import base64
import struct
import time
import unittest

from bot.solana_util import b58decode
from bot.trades import (
    OFERTA_TOTALA,
    TRADE_EVENT_DISCRIMINATOR,
    TradeTracker,
    decodeaza_trade,
    extrage_tranzactii,
)

MINT = "5tMi4JLbfYvfhSCr1Bg8mbf2iCsMRHmYeMBoJVoWpump"
USER = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"


def eveniment(mint=MINT, sol=0.1874, tokens=1_000_000, cumparare=True,
              vsol=30.0, vtok=1_070_000_000, disc=TRADE_EVENT_DISCRIMINATOR,
              coada=b"\x00" * 245):
    return (
        disc
        + b58decode(mint)
        + struct.pack("<QQ", int(sol * 1e9), int(tokens * 1e6))
        + bytes([1 if cumparare else 0])
        + b58decode(USER)
        + struct.pack("<q", 1787000000)
        + struct.pack("<QQ", int(vsol * 1e9), int(vtok * 1e6))
        + coada
    )


def linie(raw: bytes) -> str:
    return "Program data: " + base64.b64encode(raw).decode()


class TestDecodare(unittest.TestCase):
    def test_cumparare(self):
        t = decodeaza_trade(eveniment())
        self.assertIsNotNone(t)
        self.assertEqual(t.mint, MINT)
        self.assertTrue(t.cumparare)
        self.assertAlmostEqual(t.sol, 0.1874, places=6)

    def test_vanzare(self):
        self.assertFalse(decodeaza_trade(eveniment(cumparare=False)).cumparare)

    def test_pretul_vine_din_rezervele_virtuale(self):
        t = decodeaza_trade(eveniment(vsol=30.0, vtok=1_000_000_000))
        self.assertAlmostEqual(t.pret_sol, 30.0 / 1_000_000_000, places=15)

    def test_discriminator_gresit_ignorat(self):
        self.assertIsNone(decodeaza_trade(eveniment(disc=b"\x01" * 8)))

    def test_eveniment_trunchiat_ignorat(self):
        self.assertIsNone(decodeaza_trade(eveniment()[:60]))

    def test_rezerve_zero_nu_imparte_la_zero(self):
        self.assertIsNone(decodeaza_trade(eveniment(vtok=0)))

    def test_extrage_din_loguri(self):
        logs = [
            "Program 6EF8 invoke [1]",
            linie(eveniment()),
            linie(eveniment(cumparare=False)),
            "Program data: nu#e@base64",
            "Program 6EF8 success",
        ]
        t = extrage_tranzactii(logs)
        self.assertEqual(len(t), 2)
        self.assertTrue(t[0].cumparare)
        self.assertFalse(t[1].cumparare)

    def test_input_invalid(self):
        for bad in (None, "text", 42, {}, []):
            self.assertEqual(extrage_tranzactii(bad), [])


class TestTradeTracker(unittest.TestCase):
    def test_retine_doar_token_urile_urmarite(self):
        tr = TradeTracker()
        t = decodeaza_trade(eveniment())
        self.assertFalse(tr.adauga(t), "nu urmarim tokenul, nu trebuie retinut")
        tr.urmareste(MINT)
        self.assertTrue(tr.adauga(t))
        self.assertEqual(tr.tranzactii_retinute, 1)
        self.assertEqual(tr.tranzactii_vazute, 2)

    def test_masoara_cumparari_si_vanzari(self):
        tr = TradeTracker(pret_sol_usd=100.0)
        tr.urmareste(MINT)
        for _ in range(7):
            tr.adauga(decodeaza_trade(eveniment(sol=0.2)))
        for _ in range(3):
            tr.adauga(decodeaza_trade(eveniment(sol=0.1, cumparare=False)))

        m = tr.masoara(MINT)
        self.assertEqual(m["buys_m5"], 7)
        self.assertEqual(m["sells_m5"], 3)
        self.assertAlmostEqual(m["volume_m5"], (7 * 0.2 + 3 * 0.1) * 100.0, places=4)

    def test_variatia_de_pret(self):
        tr = TradeTracker()
        tr.urmareste(MINT)
        tr.adauga(decodeaza_trade(eveniment(vsol=30.0, vtok=1_000_000_000)))
        tr.adauga(decodeaza_trade(eveniment(vsol=60.0, vtok=1_000_000_000)))
        self.assertAlmostEqual(tr.masoara(MINT)["price_change_m5"], 100.0, places=3)

    def test_capitalizarea_din_pret_si_oferta(self):
        tr = TradeTracker(pret_sol_usd=94.0)
        tr.urmareste(MINT)
        tr.adauga(decodeaza_trade(eveniment(vsol=30.0, vtok=1_000_000_000)))
        asteptat = (30.0 / 1_000_000_000) * OFERTA_TOTALA * 94.0
        self.assertAlmostEqual(tr.masoara(MINT)["market_cap"], asteptat, places=4)

    def test_fereastra_uita_tranzactiile_vechi(self):
        tr = TradeTracker(fereastra_secunde=0.05)
        tr.urmareste(MINT)
        tr.adauga(decodeaza_trade(eveniment()))
        time.sleep(0.08)
        self.assertIsNone(tr.masoara(MINT), "tranzactiile vechi trebuie uitate")

    def test_uita_tokenul(self):
        tr = TradeTracker()
        tr.urmareste(MINT)
        tr.adauga(decodeaza_trade(eveniment()))
        tr.uita(MINT)
        self.assertIsNone(tr.masoara(MINT))
        self.assertEqual(tr.urmarite, 0)

    def test_token_necunoscut(self):
        self.assertIsNone(TradeTracker().masoara("altceva"))


if __name__ == "__main__":
    unittest.main()
