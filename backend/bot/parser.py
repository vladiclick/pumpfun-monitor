"""Extragerea datelor de token dintr-o tranzactie pump.fun `create`.

Strategia (in ordinea increderii):
  1. Instructiunea `create` a programului pump.fun -> mint = conturile[0],
     iar `name`/`symbol`/`uri` se citesc borsh din data instructiunii.
  2. Fallback: log-urile "Program data:" (evenimentul Anchor CreateEvent).
  3. Fallback: postTokenBalances / conturile de tip Mint initializate in tx.
"""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from typing import Any, Iterator

from .logging_setup import get_logger
from .settings import PUMPFUN_PROGRAM_ID
from .solana_util import (
    CREATE_DISCRIMINATORS,
    CREATE_EVENT_DISCRIMINATOR,
    BorshReader,
    b58decode,
    is_valid_pubkey,
)

log = get_logger("bot.parser")

SPL_TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
SPL_TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


@dataclass
class TokenInfo:
    """Rezultatul parsarii unei tranzactii de creare."""

    mint: str
    name: str = ""
    symbol: str = ""
    uri: str = ""
    creator: str = ""
    source: str = "unknown"  # din ce metoda a iesit rezultatul


def _dget(obj: Any, key: str, default: Any = None) -> Any:
    """`.get` tolerant: daca obiectul nu e dict, intoarce default in loc sa arunce.

    Nodurile RPC (mai ales cele publice, sub incarcare) pot returna structuri
    partiale; nu vrem ca botul sa cada pentru un camp de tip gresit.
    """
    return obj.get(key, default) if isinstance(obj, dict) else default


def _iter_instructions(tx: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Toate instructiunile: top-level + inner, in ordinea aparitiei."""
    message = _dget(_dget(tx, "transaction"), "message")
    for ix in _dget(message, "instructions") or []:
        if isinstance(ix, dict):
            yield ix
    for group in _dget(_dget(tx, "meta"), "innerInstructions") or []:
        for ix in _dget(group, "instructions") or []:
            if isinstance(ix, dict):
                yield ix


def _decode_create_payload(raw: bytes) -> tuple[str, str, str, str]:
    """Citeste name/symbol/uri/creator din corpul instructiunii `create`."""
    reader = BorshReader(raw, offset=8)  # sarim discriminatorul Anchor
    name = reader.read_string(max_len=256)
    symbol = reader.read_string(max_len=64)
    uri = reader.read_string(max_len=512)

    creator = ""
    # Versiunile mai noi ale programului au si `creator: Pubkey` la final.
    if reader.remaining >= 32:
        try:
            creator = reader.read_pubkey()
        except ValueError:
            creator = ""
    return name, symbol, uri, creator


def _from_create_instruction(tx: dict[str, Any]) -> TokenInfo | None:
    for ix in _iter_instructions(tx):
        if ix.get("programId") != PUMPFUN_PROGRAM_ID:
            continue
        data_b58 = ix.get("data")
        if not isinstance(data_b58, str) or not data_b58:
            continue
        try:
            raw = b58decode(data_b58)
        except ValueError:
            continue
        if len(raw) < 8 or raw[:8] not in CREATE_DISCRIMINATORS:
            continue

        accounts = [a for a in (ix.get("accounts") or []) if isinstance(a, str)]
        mint = accounts[0] if accounts else ""
        if not is_valid_pubkey(mint):
            log.debug("Instructiune create fara mint valid in accounts[0]")
            continue

        # `is_valid_pubkey` verifica doar FORMA - orice cont din instructiune ar
        # trece. Daca pump.fun reordoneaza conturile, am returna tacut alt cont
        # drept mint. Confruntam cu mint-urile pe care tranzactia chiar le-a
        # creat/atins; daca nu se potriveste, refuzam in loc sa ghicim.
        known = _mints_in_transaction(tx)
        if known and mint not in known:
            log.warning(
                "accounts[0]=%s nu apare printre mint-urile tranzactiei %s - "
                "layout-ul pump.fun s-a schimbat? Trec pe alta metoda.",
                mint[:8], ", ".join(m[:8] for m in sorted(known)),
            )
            continue

        try:
            name, symbol, uri, creator = _decode_create_payload(raw)
        except ValueError as exc:
            # Layout schimbat de pump.fun -> pastram macar mint-ul.
            log.debug("Nu pot decoda payload-ul create (%s); pastrez doar mint-ul", exc)
            name = symbol = uri = creator = ""

        if not creator:
            # In layout-ul clasic, `user` (semnatarul) e printre ultimele conturi.
            creator = _first_signer(tx)

        return TokenInfo(
            mint=mint, name=name, symbol=symbol, uri=uri,
            creator=creator, source="create_ix",
        )
    return None


def parse_create_logs(logs: Any, signer: str = "") -> TokenInfo | None:
    """Extrage tokenul DIRECT din logurile primite pe WebSocket - fara RPC.

    Programul pump.fun emite evenimentul Anchor `CreateEvent` ca
    "Program data: <base64>", iar acesta contine deja name/symbol/uri/mint.
    E calea rapida: economisim 1-4 apeluri getTransaction per token, ceea ce
    conteaza enorm pe un RPC public unde acele apeluri sunt gatuirea reala.
    """
    if not isinstance(logs, list):
        return None

    for line in logs:
        if not isinstance(line, str) or not line.startswith("Program data:"):
            continue
        payload = line.split("Program data:", 1)[1].strip()
        try:
            raw = base64.b64decode(payload, validate=True)
        except (ValueError, binascii.Error):
            continue

        # Filtram dupa discriminator: in aceleasi loguri apare si TradeEvent.
        if len(raw) < 8 or raw[:8] != CREATE_EVENT_DISCRIMINATOR:
            continue

        reader = BorshReader(raw, offset=8)
        try:
            name = reader.read_string(max_len=256)
            symbol = reader.read_string(max_len=64)
            uri = reader.read_string(max_len=512)
            mint = reader.read_pubkey()
        except ValueError as exc:
            log.debug("CreateEvent neparsabil: %s", exc)
            continue

        if not is_valid_pubkey(mint):
            continue

        creator = signer
        try:
            reader.read_pubkey()          # bondingCurve
            creator = reader.read_pubkey() or signer  # user / creator
        except ValueError:
            pass

        return TokenInfo(
            mint=mint, name=name, symbol=symbol, uri=uri,
            creator=creator, source="ws_logs" if not signer else "program_data",
        )
    return None


def _mints_in_transaction(tx: dict[str, Any]) -> set[str]:
    """Mint-urile pe care tranzactia le-a creat sau atins, dupa date independente.

    Sursele (`initializeMint*` si `postTokenBalances`) sunt produse de programul
    SPL Token, nu de pump.fun, deci nu se schimba daca pump.fun isi rearanjeaza
    conturile. Folosite doar ca verificare incrucisata.
    """
    found: set[str] = set()

    for ix in _iter_instructions(tx):
        if ix.get("programId") not in (SPL_TOKEN_PROGRAM, SPL_TOKEN_2022_PROGRAM):
            continue
        parsed = ix.get("parsed")
        if not isinstance(parsed, dict):
            continue
        if parsed.get("type") in ("initializeMint", "initializeMint2"):
            mint = (parsed.get("info") or {}).get("mint")
            if is_valid_pubkey(mint):
                found.add(mint)

    for bal in _dget(_dget(tx, "meta"), "postTokenBalances") or []:
        if isinstance(bal, dict) and is_valid_pubkey(bal.get("mint")):
            found.add(bal["mint"])

    return found


def _from_program_data_logs(tx: dict[str, Any]) -> TokenInfo | None:
    """Aceeasi extractie, dar pornind de la o tranzactie deja descarcata."""
    logs = _dget(_dget(tx, "meta"), "logMessages") or []
    return parse_create_logs(logs, signer=_first_signer(tx))


def _from_token_balances(tx: dict[str, Any]) -> TokenInfo | None:
    """Ultim fallback: mint-ul nou aparut in postTokenBalances sau in initializeMint."""
    meta = _dget(tx, "meta") or {}

    for ix in _iter_instructions(tx):
        if ix.get("programId") not in (SPL_TOKEN_PROGRAM, SPL_TOKEN_2022_PROGRAM):
            continue
        parsed = ix.get("parsed")
        if not isinstance(parsed, dict):
            continue
        if parsed.get("type") in ("initializeMint", "initializeMint2"):
            mint = (parsed.get("info") or {}).get("mint")
            if is_valid_pubkey(mint):
                return TokenInfo(mint=mint, creator=_first_signer(tx), source="initialize_mint")

    pre = {b.get("mint") for b in (_dget(meta, "preTokenBalances") or []) if isinstance(b, dict)}
    for bal in _dget(meta, "postTokenBalances") or []:
        if not isinstance(bal, dict):
            continue
        mint = bal.get("mint")
        if is_valid_pubkey(mint) and mint not in pre:
            return TokenInfo(mint=mint, creator=_first_signer(tx), source="post_balances")
    return None


def _first_signer(tx: dict[str, Any]) -> str:
    """Prima cheie semnatara din tranzactie (de obicei creatorul tokenului)."""
    keys = _dget(_dget(_dget(tx, "transaction"), "message"), "accountKeys") or []
    for key in keys:
        if isinstance(key, dict) and key.get("signer"):
            pubkey = key.get("pubkey")
            if is_valid_pubkey(pubkey):
                return pubkey
        elif isinstance(key, str) and is_valid_pubkey(key):
            return key
    return ""


def parse_create_transaction(tx: dict[str, Any] | None) -> TokenInfo | None:
    """Punct unic de intrare. Returneaza None daca tranzactia nu e utilizabila."""
    if not isinstance(tx, dict):
        return None

    # Tranzactiile esuate nu ne intereseaza - tokenul nu exista efectiv.
    err = _dget(_dget(tx, "meta"), "err")
    if err:
        log.debug("Tranzactie esuata on-chain, o ignor: %s", err)
        return None

    # `_from_program_data_logs` citeste evenimentul Anchor, unde mint-ul e
    # identificat prin POZITIA IN STRUCTURA borsh, nu prin indexul unui cont.
    # E imun la o reordonare a conturilor instructiunii, deci merge primul.
    for extractor in (_from_program_data_logs, _from_create_instruction, _from_token_balances):
        try:
            info = extractor(tx)
        except Exception as exc:  # niciun parser nu are voie sa doboare botul
            log.warning("Parser %s a aruncat %s: %s", extractor.__name__, type(exc).__name__, exc)
            continue
        if info is not None:
            return info
    return None


def parse_mint_account(value: dict[str, Any] | None) -> dict[str, Any] | None:
    """Extrage mintAuthority / freezeAuthority / supply din getAccountInfo(jsonParsed)."""
    if not isinstance(value, dict):
        return None

    owner = value.get("owner")
    if owner not in (SPL_TOKEN_PROGRAM, SPL_TOKEN_2022_PROGRAM):
        return None

    data = value.get("data")
    if not isinstance(data, dict):
        # Nodul a raspuns cu base64 (nu stie sa parseze) - nu putem decide.
        return None

    parsed = data.get("parsed")
    if not isinstance(parsed, dict) or parsed.get("type") != "mint":
        return None

    info = parsed.get("info")
    if not isinstance(info, dict):
        return None

    # Cheile TREBUIE sa fie prezente, chiar daca valoarea e null.
    # `info.get("mintAuthority")` intoarce None si cand cheia lipseste, iar
    # None inseamna "autoritate revocata" => un raspuns trunchiat ar fi citit
    # ca "token curat" si ar produce un PASS fals. Preferam ERROR.
    required = ("mintAuthority", "freezeAuthority", "decimals")
    missing = [k for k in required if k not in info]
    if missing:
        log.warning(
            "Cont de mint fara campurile %s - il tratez ca neconcludent, nu ca revocat",
            ", ".join(missing),
        )
        return None

    return {
        "mint_authority": info.get("mintAuthority"),
        "freeze_authority": info.get("freezeAuthority"),
        "supply": info.get("supply"),
        "decimals": info.get("decimals"),
        "is_initialized": info.get("isInitialized", True),
        "program": "token-2022" if owner == SPL_TOKEN_2022_PROGRAM else "spl-token",
    }
