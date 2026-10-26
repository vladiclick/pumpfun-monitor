"""Teste pentru parsarea tranzactiilor pump.fun (fara retea)."""
from __future__ import annotations

import base64
import struct
import unittest

from bot.parser import parse_create_logs, parse_create_transaction, parse_mint_account
from bot.settings import PUMPFUN_PROGRAM_ID
from bot.solana_util import (
    CREATE_EVENT_DISCRIMINATOR,
    CREATE_IX_DISCRIMINATOR,
    CREATE_V2_IX_DISCRIMINATOR,
    anchor_discriminator,
    b58decode,
    b58encode,
)

MINT = "5tMi4JLbfYvfhSCr1Bg8mbf2iCsMRHmYeMBoJVoWpump"
CREATOR = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"


def _borsh_str(value: str) -> bytes:
    raw = value.encode("utf-8")
    return struct.pack("<I", len(raw)) + raw


def _create_ix_data(name: str, symbol: str, uri: str, disc: bytes = CREATE_IX_DISCRIMINATOR) -> str:
    payload = disc + _borsh_str(name) + _borsh_str(symbol) + _borsh_str(uri)
    return b58encode(payload)


def _tx(instructions, logs=None, err=None):
    return {
        "meta": {"err": err, "logMessages": logs or [], "innerInstructions": []},
        "transaction": {
            "message": {
                "accountKeys": [{"pubkey": CREATOR, "signer": True, "writable": True}],
                "instructions": instructions,
            }
        },
    }


class TestCreateInstructionParsing(unittest.TestCase):
    def test_extrage_mint_nume_simbol(self):
        tx = _tx([
            {
                "programId": PUMPFUN_PROGRAM_ID,
                "accounts": [MINT, "AnotherAccount1111111111111111111111111111"],
                "data": _create_ix_data("Test Token", "TEST", "https://ipfs.io/x"),
            }
        ])
        info = parse_create_transaction(tx)
        self.assertIsNotNone(info)
        self.assertEqual(info.mint, MINT)
        self.assertEqual(info.name, "Test Token")
        self.assertEqual(info.symbol, "TEST")
        self.assertEqual(info.source, "create_ix")
        self.assertEqual(info.creator, CREATOR)

    def test_accepta_si_create_v2(self):
        """pump.fun a migrat la CreateV2 - discriminatorul difera, layout-ul nu."""
        tx = _tx([
            {
                "programId": PUMPFUN_PROGRAM_ID,
                "accounts": [MINT],
                "data": _create_ix_data("Cybara AI", "CYB", "https://x/y",
                                        disc=CREATE_V2_IX_DISCRIMINATOR),
            }
        ])
        info = parse_create_transaction(tx)
        self.assertIsNotNone(info)
        self.assertEqual(info.mint, MINT)
        self.assertEqual(info.symbol, "CYB")
        self.assertEqual(info.source, "create_ix")

    def test_discriminator_necunoscut_e_ignorat(self):
        tx = _tx([{"programId": PUMPFUN_PROGRAM_ID, "accounts": [MINT],
                   "data": _create_ix_data("X", "X", "u", disc=b"\x00" * 8)}])
        self.assertIsNone(parse_create_transaction(tx))

    def test_nume_cu_emoji_nu_crapa(self):
        tx = _tx([
            {
                "programId": PUMPFUN_PROGRAM_ID,
                "accounts": [MINT],
                "data": _create_ix_data("Pepe 🐸 Coin", "PEPE🐸", "ipfs://q"),
            }
        ])
        info = parse_create_transaction(tx)
        self.assertEqual(info.symbol, "PEPE🐸")

    def test_payload_trunchiat_pastreaza_mintul(self):
        # Simulam o schimbare de layout: date insuficiente dupa discriminator.
        data = b58encode(CREATE_IX_DISCRIMINATOR + b"\x05\x00\x00\x00ab")
        tx = _tx([{"programId": PUMPFUN_PROGRAM_ID, "accounts": [MINT], "data": data}])
        info = parse_create_transaction(tx)
        self.assertIsNotNone(info)
        self.assertEqual(info.mint, MINT)
        self.assertEqual(info.name, "")

    def test_tranzactie_esuata_e_ignorata(self):
        tx = _tx(
            [{"programId": PUMPFUN_PROGRAM_ID, "accounts": [MINT],
              "data": _create_ix_data("X", "X", "u")}],
            err={"InstructionError": [0, "Custom"]},
        )
        self.assertIsNone(parse_create_transaction(tx))

    def test_alt_program_e_ignorat(self):
        tx = _tx([{"programId": TOKEN_PROGRAM, "accounts": [MINT], "data": "abc"}])
        self.assertIsNone(parse_create_transaction(tx))

    def test_fallback_initialize_mint(self):
        tx = _tx([
            {
                "programId": TOKEN_PROGRAM,
                "parsed": {"type": "initializeMint2", "info": {"mint": MINT}},
            }
        ])
        info = parse_create_transaction(tx)
        self.assertIsNotNone(info)
        self.assertEqual(info.mint, MINT)
        self.assertEqual(info.source, "initialize_mint")

    def test_fallback_post_token_balances(self):
        tx = _tx([])
        tx["meta"]["preTokenBalances"] = []
        tx["meta"]["postTokenBalances"] = [{"mint": MINT}]
        info = parse_create_transaction(tx)
        self.assertEqual(info.mint, MINT)
        self.assertEqual(info.source, "post_balances")

    def test_refuza_accounts0_daca_nu_e_un_mint_al_tranzactiei(self):
        """Daca pump.fun reordoneaza conturile, nu avem voie sa returnam tacut
        alt cont drept mint. Verificam incrucisat cu postTokenBalances."""
        alt_cont = "TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM"
        tx = _tx([
            {
                "programId": PUMPFUN_PROGRAM_ID,
                "accounts": [alt_cont, MINT],   # ordine inversata
                "data": _create_ix_data("Test", "TST", "u"),
            }
        ])
        tx["meta"]["preTokenBalances"] = []
        tx["meta"]["postTokenBalances"] = [{"mint": MINT}]

        info = parse_create_transaction(tx)
        self.assertIsNotNone(info)
        self.assertEqual(info.mint, MINT, "trebuia sa cada pe metoda independenta")
        self.assertNotEqual(info.mint, alt_cont)

    def test_accepta_accounts0_cand_se_confirma(self):
        tx = _tx([
            {
                "programId": PUMPFUN_PROGRAM_ID,
                "accounts": [MINT],
                "data": _create_ix_data("Test", "TST", "u"),
            }
        ])
        tx["meta"]["preTokenBalances"] = []
        tx["meta"]["postTokenBalances"] = [{"mint": MINT}]
        info = parse_create_transaction(tx)
        self.assertEqual(info.mint, MINT)
        self.assertEqual(info.source, "create_ix")

    def test_evenimentul_din_loguri_are_prioritate(self):
        """Evenimentul Anchor identifica mint-ul prin pozitie in structura
        borsh, nu prin indexul unui cont - deci e imun la reordonari."""
        tx = _tx(
            [{"programId": PUMPFUN_PROGRAM_ID, "accounts": ["NuEsteUnMintValid"],
              "data": _create_ix_data("Gresit", "BAD", "u")}],
            logs=[_create_event_log(name="Corect", symbol="OK")],
        )
        info = parse_create_transaction(tx)
        self.assertEqual(info.mint, MINT)
        self.assertEqual(info.symbol, "OK")

    def test_input_gunoi_nu_arunca(self):
        for bad in (None, {}, {"meta": None}, {"transaction": "nope"}, []):
            self.assertIsNone(parse_create_transaction(bad))  # type: ignore[arg-type]

    def test_data_base58_invalida(self):
        tx = _tx([{"programId": PUMPFUN_PROGRAM_ID, "accounts": [MINT], "data": "0OIl!!"}])
        self.assertIsNone(parse_create_transaction(tx))


def _create_event_log(name="Quant Zane", symbol="QZ", uri="https://x/y",
                      mint=MINT, bonding=CREATOR, creator=CREATOR,
                      disc=CREATE_EVENT_DISCRIMINATOR):
    """Reconstruieste linia `Program data:` cu evenimentul Anchor CreateEvent."""
    payload = (
        disc
        + _borsh_str(name) + _borsh_str(symbol) + _borsh_str(uri)
        + b58decode(mint) + b58decode(bonding) + b58decode(creator)
        + b"\x00" * 8  # campuri suplimentare (timestamp etc.)
    )
    return "Program data: " + base64.b64encode(payload).decode()


class TestCreateEventLogs(unittest.TestCase):
    """Calea rapida: token extras direct din logurile WebSocket, fara RPC."""

    def test_extrage_din_loguri(self):
        info = parse_create_logs([
            "Program 6EF8... invoke [1]",
            _create_event_log(),
            "Program 6EF8... success",
        ])
        self.assertIsNotNone(info)
        self.assertEqual(info.mint, MINT)
        self.assertEqual(info.name, "Quant Zane")
        self.assertEqual(info.symbol, "QZ")
        self.assertEqual(info.creator, CREATOR)
        self.assertEqual(info.source, "ws_logs")

    def test_ignora_trade_event(self):
        """In aceleasi loguri apar si TradeEvent-uri; nu trebuie confundate."""
        trade = anchor_discriminator("event", "TradeEvent")
        self.assertIsNone(parse_create_logs([_create_event_log(disc=trade)]))

    def test_base64_stricat_e_sarit(self):
        logs = ["Program data: nu#este@base64", _create_event_log()]
        self.assertEqual(parse_create_logs(logs).mint, MINT)

    def test_fara_program_data(self):
        self.assertIsNone(parse_create_logs(["Program log: Instruction: CreateV2"]))

    def test_input_invalid(self):
        for bad in (None, "text", 42, {}):
            self.assertIsNone(parse_create_logs(bad))

    def test_mint_invalid_e_respins(self):
        payload = (
            CREATE_EVENT_DISCRIMINATOR
            + _borsh_str("n") + _borsh_str("s") + _borsh_str("u")
            + b"\x01" * 4  # prea putini bytes pentru un pubkey
        )
        line = "Program data: " + base64.b64encode(payload).decode()
        self.assertIsNone(parse_create_logs([line]))


class TestMintAccountParsing(unittest.TestCase):
    def _account(self, mint_auth, freeze_auth):
        return {
            "owner": TOKEN_PROGRAM,
            "data": {
                "program": "spl-token",
                "parsed": {
                    "type": "mint",
                    "info": {
                        "mintAuthority": mint_auth,
                        "freezeAuthority": freeze_auth,
                        "supply": "1000000000000000",
                        "decimals": 6,
                        "isInitialized": True,
                    },
                },
            },
        }

    def test_autoritati_revocate(self):
        info = parse_mint_account(self._account(None, None))
        self.assertIsNone(info["mint_authority"])
        self.assertIsNone(info["freeze_authority"])
        self.assertEqual(info["program"], "spl-token")

    def test_autoritate_activa(self):
        info = parse_mint_account(self._account(CREATOR, None))
        self.assertEqual(info["mint_authority"], CREATOR)

    def test_chei_lipsa_nu_inseamna_revocat(self):
        """`info.get("mintAuthority")` intoarce None si cand cheia LIPSESTE.

        Interpretat ca "revocata", un raspuns trunchiat ar fi produs un PASS
        fals - cel mai prost mod de esec pentru un filtru anti-rug.
        """
        fara_mint_auth = {
            "owner": TOKEN_PROGRAM,
            "data": {"parsed": {"type": "mint", "info": {
                "freezeAuthority": None, "supply": "1", "decimals": 6,
            }}},
        }
        self.assertIsNone(parse_mint_account(fara_mint_auth))

        fara_freeze = {
            "owner": TOKEN_PROGRAM,
            "data": {"parsed": {"type": "mint", "info": {
                "mintAuthority": None, "supply": "1", "decimals": 6,
            }}},
        }
        self.assertIsNone(parse_mint_account(fara_freeze))
        self.assertIsNone(parse_mint_account(
            {"owner": TOKEN_PROGRAM, "data": {"parsed": {"type": "mint", "info": {}}}}
        ))

    def test_null_explicit_inseamna_revocat(self):
        """Cheia prezenta cu valoarea null = autoritate chiar revocata."""
        info = parse_mint_account(self._account(None, None))
        self.assertIsNotNone(info)
        self.assertIsNone(info["mint_authority"])

    def test_cont_nevalid(self):
        self.assertIsNone(parse_mint_account(None))
        self.assertIsNone(parse_mint_account({"owner": "altcineva"}))
        self.assertIsNone(parse_mint_account({"owner": TOKEN_PROGRAM, "data": ["base64", "base64"]}))


if __name__ == "__main__":
    unittest.main()
