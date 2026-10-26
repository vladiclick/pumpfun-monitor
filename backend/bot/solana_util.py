"""Utilitare Solana pure-python: base58, borsh minimal, discriminatori Anchor.

Folosim `solders` daca e instalat (validare rapida de Pubkey), dar totul are
fallback pur-python ca botul sa nu moara daca lipseste o dependenta optionala.
"""
from __future__ import annotations

import hashlib
import struct

try:  # pragma: no cover - depinde de mediu
    from solders.pubkey import Pubkey  # type: ignore

    _HAS_SOLDERS = True
except Exception:  # ImportError sau ABI mismatch pe versiuni exotice de Python
    Pubkey = None  # type: ignore
    _HAS_SOLDERS = False

try:  # pragma: no cover
    import base58 as _base58

    _HAS_BASE58 = True
except Exception:
    _base58 = None  # type: ignore
    _HAS_BASE58 = False

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58_ALPHABET)}


def b58decode(data: str) -> bytes:
    """Decodeaza base58 (Bitcoin alphabet). Ridica ValueError la input invalid."""
    if not isinstance(data, str) or not data:
        raise ValueError("input base58 gol sau de tip gresit")

    if _HAS_BASE58:
        try:
            return _base58.b58decode(data)
        except Exception as exc:  # biblioteca arunca tipuri proprii
            raise ValueError(f"base58 invalid: {exc}") from exc

    # Fallback pur-python (acelasi algoritm ca biblioteca).
    num = 0
    for ch in data:
        idx = _B58_INDEX.get(ch)
        if idx is None:
            raise ValueError(f"caracter base58 invalid: {ch!r}")
        num = num * 58 + idx

    raw = num.to_bytes((num.bit_length() + 7) // 8, "big") if num else b""
    pad = len(data) - len(data.lstrip("1"))
    return b"\x00" * pad + raw


def is_valid_pubkey(value: str) -> bool:
    """True daca sirul e o adresa Solana valida (32 bytes base58)."""
    if not value or not isinstance(value, str) or not (32 <= len(value) <= 44):
        return False
    if _HAS_SOLDERS:
        try:
            Pubkey.from_string(value)  # type: ignore[union-attr]
            return True
        except Exception:
            return False
    try:
        return len(b58decode(value)) == 32
    except ValueError:
        return False


def anchor_discriminator(namespace: str, name: str) -> bytes:
    """Primii 8 bytes din sha256("<namespace>:<name>") - conventia Anchor."""
    return hashlib.sha256(f"{namespace}:{name}".encode()).digest()[:8]


# Discriminatorii instructiunilor de creare din programul pump.fun.
# Programul a migrat de la `create` la `create_v2`; le acceptam pe amandoua,
# ca botul sa mearga si pe token-uri vechi, si pe cele noi.
CREATE_IX_DISCRIMINATOR = anchor_discriminator("global", "create")        # 181ec828051c0777
CREATE_V2_IX_DISCRIMINATOR = anchor_discriminator("global", "create_v2")  # d6904cec5f8b31b4
CREATE_DISCRIMINATORS = (CREATE_IX_DISCRIMINATOR, CREATE_V2_IX_DISCRIMINATOR)

# Discriminatorul evenimentului Anchor `CreateEvent`, emis in loguri ca
# "Program data: <base64>". Contine nume, simbol, uri si mint - adica tot ce ne
# trebuie, fara niciun apel RPC.
CREATE_EVENT_DISCRIMINATOR = anchor_discriminator("event", "CreateEvent")  # 1b72a94ddeeb6376


class BorshReader:
    """Cititor borsh minimal: doar ce ne trebuie (string, pubkey, u64)."""

    def __init__(self, data: bytes, offset: int = 0) -> None:
        self.data = data
        self.offset = offset

    @property
    def remaining(self) -> int:
        return len(self.data) - self.offset

    def read_bytes(self, count: int) -> bytes:
        if count < 0 or self.remaining < count:
            raise ValueError(f"borsh: date insuficiente (cer {count}, am {self.remaining})")
        chunk = self.data[self.offset : self.offset + count]
        self.offset += count
        return chunk

    def read_u32(self) -> int:
        return struct.unpack("<I", self.read_bytes(4))[0]

    def read_string(self, max_len: int = 4096) -> str:
        """String borsh: u32 little-endian cu lungimea, apoi bytes UTF-8."""
        length = self.read_u32()
        if length > max_len:
            raise ValueError(f"borsh: string absurd de lung ({length} bytes)")
        raw = self.read_bytes(length)
        # Numele de token-uri contin frecvent emoji / bytes stricate -> nu crapam.
        return raw.decode("utf-8", errors="replace").strip()

    def read_pubkey(self) -> str:
        raw = self.read_bytes(32)
        if _HAS_SOLDERS:
            try:
                return str(Pubkey(raw))  # type: ignore[misc]
            except Exception:
                pass
        return b58encode(raw)


def b58encode(raw: bytes) -> str:
    """Encodeaza bytes in base58 (folosit doar la fallback)."""
    if _HAS_BASE58:
        return _base58.b58encode(raw).decode()  # type: ignore[union-attr]

    num = int.from_bytes(raw, "big")
    out = ""
    while num > 0:
        num, rem = divmod(num, 58)
        out = _B58_ALPHABET[rem] + out
    pad = len(raw) - len(raw.lstrip(b"\x00"))
    return "1" * pad + out


def shorten(address: str, head: int = 4, tail: int = 4) -> str:
    """Scurteaza o adresa pentru afisare: AbCd...WxYz."""
    if not address or len(address) <= head + tail + 3:
        return address or ""
    return f"{address[:head]}...{address[-tail:]}"
