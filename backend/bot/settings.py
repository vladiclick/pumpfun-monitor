"""Setari globale citite din .env (valabile atat pentru server cat si pentru boti)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from dotenv import load_dotenv

# Radacina proiectului backend/ - toate caile relative pornesc de aici.
BASE_DIR = Path(__file__).resolve().parent.parent

# load_dotenv nu suprascrie variabilele deja setate in mediu (util in Docker/CI).
load_dotenv(BASE_DIR / ".env", override=False)

# Programul pump.fun pe mainnet. Constanta, nu se configureaza.
PUMPFUN_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"


def _derive_ws_url(http_url: str) -> str:
    """Transforma un endpoint HTTP(S) de RPC in echivalentul WebSocket.

    api.mainnet-beta.solana.com foloseste acelasi host pentru ws, deci
    e suficient sa schimbam schema. Providerii care au host separat pentru
    ws (ex. unele planuri Helius) trebuie sa seteze explicit SOLANA_RPC_WS.
    """
    try:
        parts = urlparse(http_url)
    except ValueError:  # URL complet invalid
        return "wss://api.mainnet-beta.solana.com"

    scheme = "wss" if parts.scheme == "https" else "ws"
    return urlunparse(parts._replace(scheme=scheme))


def mask_endpoint(url: str) -> str:
    """Ascunde orice secret dintr-un URL de RPC, pastrand doar schema + host.

    Providerii isi pun cheia in locuri diferite: Helius in query (`?api-key=`),
    QuickNode/Alchemy/Ankr in calea URL-ului. Nu incercam sa le enumeram - taiem
    tot ce nu e schema si host, deci nu putem scapa un secret dintr-un provider
    pe care nu l-am prevazut.
    """
    if not url or not isinstance(url, str):
        return ""
    try:
        parts = urlparse(url)
    except ValueError:
        return "***"
    if not parts.scheme or not parts.hostname:
        return "***"

    host = parts.hostname
    if parts.port:
        host = f"{host}:{parts.port}"

    hidden = bool(parts.path.strip("/")) or bool(parts.query) or bool(parts.username)
    return f"{parts.scheme}://{host}{'/***' if hidden else ''}"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "da"}


@dataclass(frozen=True)
class Settings:
    rpc_http: str
    rpc_ws: str
    backend_url: str
    ingest_token: str
    db_path: Path
    log_level: str
    cors_origins: list[str]
    dashboard_token: str
    telegram_bot_token: str
    telegram_chat_id: str


def load_settings() -> Settings:
    """Citeste si normalizeaza configuratia globala."""
    rpc_http = os.getenv("SOLANA_RPC_HTTP", "https://api.mainnet-beta.solana.com").strip()
    if not rpc_http:
        rpc_http = "https://api.mainnet-beta.solana.com"

    rpc_ws = os.getenv("SOLANA_RPC_WS", "").strip() or _derive_ws_url(rpc_http)

    db_raw = os.getenv("DB_PATH", "data/events.db").strip() or "data/events.db"
    db_path = Path(db_raw)
    if not db_path.is_absolute():
        db_path = BASE_DIR / db_path

    origins_raw = os.getenv("CORS_ORIGINS", "*").strip() or "*"
    origins = [o.strip() for o in origins_raw.split(",") if o.strip()]

    return Settings(
        rpc_http=rpc_http,
        rpc_ws=rpc_ws,
        backend_url=os.getenv("BACKEND_URL", "http://127.0.0.1:8000").strip().rstrip("/"),
        ingest_token=os.getenv("INGEST_TOKEN", "dev-token").strip(),
        db_path=db_path,
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
        cors_origins=origins,
        # Gol = dashboard deschis (comportamentul implicit, local).
        # Setat = REST-ul si WebSocket-ul cer acest token.
        dashboard_token=os.getenv("DASHBOARD_TOKEN", "").strip(),
        # Notificari Telegram, optionale. Token gol = functie dezactivata curat.
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
    )


SETTINGS = load_settings()
