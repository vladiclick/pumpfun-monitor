"""Notificari Telegram pentru tokenurile care trec filtrele botului.

Functionalitate optionala: e activa doar daca in .env sunt setate atat
TELEGRAM_BOT_TOKEN cat si TELEGRAM_CHAT_ID. Daca lipseste unul dintre ele,
TelegramNotifier ramane dezactivat si toate metodele publice devin no-op -
botul de monitorizare merge mai departe normal, doar fara notificari.

Trimiterea e non-blocanta si nu are cum sa afecteze restul botului:
notify_pass() doar pune mesajul intr-o coada in memorie, iar un worker de
fundal il trimite catre Bot API cu retry (acelasi model ca in emitter.py -
coada marginita, cele mai vechi mesaje sunt aruncate daca se umple, workerul
nu are voie sa moara niciodata).
"""
from __future__ import annotations

import asyncio
import contextlib
import html
from typing import Any

import httpx

from .logging_setup import get_logger

log = get_logger("bot.telegram")

TELEGRAM_API = "https://api.telegram.org"


class TelegramError(RuntimeError):
    """Eroare la comunicarea cu Bot API."""


def format_pass_message(
    *, bot_id: str, name: str, symbol: str, mint: str, latency_ms: int | None,
    heat: dict | None = None,
) -> str:
    """Mesajul HTML trimis pentru un token care a trecut filtrele.

    Tot ce vine din exterior (numele/simbolul unui token pot contine <, >, &,
    emoji sau orice text arbitrar) trece prin html.escape - altfel un nume
    "creativ" ar strica parse_mode=HTML si mesajul n-ar mai ajunge deloc.
    """
    safe_name = html.escape(name or "?")
    safe_symbol = html.escape(symbol or "?")
    safe_bot_id = html.escape(bot_id or "?")
    safe_mint = html.escape(mint or "")
    latency = f"{latency_ms} ms" if latency_ms is not None else "N/A"
    # Phantom primul: pe telefon linkul deschide direct APLICATIA, nu browserul.
    # Domeniul conteaza: phantom.com publica /.well-known/apple-app-site-association
    # (si assetlinks.json pe Android) cu calea /tokens/* legata de aplicatie.
    # trade.phantom.com NU are fisierul (404), deci de acolo sistemul de operare
    # nu poate deschide aplicatia si linkul cade in browser.
    phantom = f"https://phantom.com/tokens/solana/{safe_mint}"
    link = f"https://pump.fun/coin/{safe_mint}"
    # Randul de "de ce e fierbinte", cand etapa de supraveghere a produs date.
    rand_foc = ""
    if isinstance(heat, dict):
        bucati = []
        mcap = heat.get("market_cap")
        if isinstance(mcap, (int, float)):
            bucati.append(f"cap. ${mcap:,.0f}")
        var = heat.get("price_change_m5")
        if isinstance(var, (int, float)):
            bucati.append(f"{var:+.0f}% in 5 min")
        cumparari, vanzari = heat.get("buys_m5"), heat.get("sells_m5")
        if isinstance(cumparari, int) and isinstance(vanzari, int):
            bucati.append(f"{cumparari} cumparari / {vanzari} vanzari")
        vol = heat.get("volume_m5")
        if isinstance(vol, (int, float)):
            bucati.append(f"volum 5 min ${vol:,.0f}")
        if bucati:
            rand_foc = "\U0001f525 " + html.escape("  ·  ".join(bucati)) + "\n"

    return (
        f"\U0001f7e2 <b>{safe_name}</b> (${safe_symbol})\n"
        f"{rand_foc}"
        f"<code>{safe_mint}</code>\n"
        f"⏱ Latenta: {latency}\n"
        f"\U0001f916 Bot: {safe_bot_id}\n"
        f'\U0001f47b <a href="{phantom}">Deschide in Phantom</a>'
        f'   \u00b7   <a href="{link}">pump.fun</a>'
    )


async def discover_chat_id(token: str, *, timeout: float = 10.0) -> list[dict]:
    """Cheama getUpdates si intoarce chat-urile gasite, cel mai recent primul.

    Fiecare element: {"chat_id": int, "type": str, "title": str, "from": str}.
    Ridica TelegramError daca API-ul refuza sau reteaua pica. Parsarea e
    defensiva peste tot - raspunsul vine de la o retea externa, deci orice
    camp poate lipsi sau poate avea alt tip decat ne asteptam.
    """
    url = f"{TELEGRAM_API}/bot{token}/getUpdates"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, params={"timeout": 0, "limit": 100})
    except httpx.HTTPError as exc:
        raise TelegramError(f"Nu pot contacta Telegram: {exc}") from exc

    try:
        data = resp.json()
    except ValueError as exc:
        raise TelegramError(f"Raspuns invalid de la Telegram (nu e JSON): {exc}") from exc

    if not isinstance(data, dict) or not data.get("ok"):
        desc = data.get("description") if isinstance(data, dict) else None
        raise TelegramError(desc or f"Telegram a refuzat cererea (HTTP {resp.status_code})")

    results = data.get("result")
    if not isinstance(results, list):
        return []

    chats: dict[int, dict] = {}
    last_seen: dict[int, int] = {}
    for idx, update in enumerate(results):
        if not isinstance(update, dict):
            continue
        msg = update.get("message")
        if not isinstance(msg, dict):
            msg = update.get("edited_message")
        if not isinstance(msg, dict):
            msg = update.get("channel_post")
        if not isinstance(msg, dict):
            continue

        chat = msg.get("chat")
        if not isinstance(chat, dict):
            continue
        chat_id = chat.get("id")
        if not isinstance(chat_id, int):
            continue

        chat_type = chat.get("type")
        if not isinstance(chat_type, str):
            chat_type = "unknown"

        title = chat.get("title") or chat.get("username") or chat.get("first_name") or ""
        if not isinstance(title, str):
            title = str(title)

        from_obj = msg.get("from")
        from_name: Any = ""
        if isinstance(from_obj, dict):
            from_name = from_obj.get("username") or from_obj.get("first_name") or ""
        if not isinstance(from_name, str):
            from_name = str(from_name)

        chats[chat_id] = {"chat_id": chat_id, "type": chat_type, "title": title, "from": from_name}
        last_seen[chat_id] = idx

    ordered_ids = sorted(last_seen, key=lambda cid: last_seen[cid], reverse=True)
    return [chats[cid] for cid in ordered_ids]


class TelegramNotifier:
    """Trimite notificari Telegram pentru tokenurile care trec filtrele.

    Fire-and-forget: notify_pass() nu asteapta niciodata dupa retea. Un
    worker de fundal preia mesajele din coada si le trimite cu retry, iar o
    eroare Telegram (retea, credentiale gresite, rate-limit) nu are voie sa
    opreasca sau sa incetineasca restul botului.
    """

    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        timeout: float = 8.0,
        queue_size: int = 200,
    ) -> None:
        self._token = (token or "").strip()
        self._chat_id = (chat_id or "").strip()
        self._timeout = timeout
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=queue_size)
        self._client: httpx.AsyncClient | None = None
        self._task: asyncio.Task | None = None
        # Pauza initiala intre reincercari. Atribut, ca sa poata fi micsorat
        # in teste fara sa schimbam comportamentul din productie.
        self._backoff_base = 1.0
        self._stopped = asyncio.Event()
        self._sent = 0
        self._failed = 0
        self._dropped = 0

    # ------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        """Activ doar daca avem si token si chat_id (dupa strip)."""
        return bool(self._token) and bool(self._chat_id)

    # ------------------------------------------------------------------
    def start(self) -> None:
        if not self.enabled:
            return
        # Clientul e creat aici, nu in __init__, tocmai ca sa nu existe
        # niciodata un httpx.AsyncClient cand feature-ul e dezactivat.
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(self._timeout, connect=3.0))
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._worker(), name="telegram-notifier")

    async def stop(self, drain_timeout: float = 3.0) -> None:
        """Incearca sa dreneze coada, apoi inchide clientul.

        Sigur de apelat daca notifierul n-a fost pornit niciodata sau daca
        e apelat de doua ori."""
        if not self.enabled:
            return
        self._stopped.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(self._task, timeout=drain_timeout)
            if not self._task.done():
                self._task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._task
            self._task = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        if self._dropped or self._failed:
            log.info(
                "Telegram notifier oprit (trimise=%d, esuate=%d, pierdute=%d)",
                self._sent, self._failed, self._dropped,
            )

    # ------------------------------------------------------------------
    def notify_pass(
        self, *, bot_id: str, name: str, symbol: str, mint: str,
        latency_ms: int | None, heat: dict | None = None,
    ) -> None:
        """Non-blocant: pune mesajul in coada si iese imediat.

        Nu e async, nu asteapta dupa nimic si nu arunca niciodata catre
        apelant. Daca e dezactivat, e no-op. Daca coada e plina, arunca cel
        mai vechi mesaj (acelasi comportament ca in emitter.py)."""
        if not self.enabled:
            return
        try:
            text = format_pass_message(
                bot_id=bot_id, name=name, symbol=symbol, mint=mint,
                latency_ms=latency_ms, heat=heat
            )
            payload = {
                "chat_id": self._chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            try:
                self._queue.put_nowait(payload)
            except asyncio.QueueFull:
                with contextlib.suppress(asyncio.QueueEmpty):
                    self._queue.get_nowait()
                    self._dropped += 1
                with contextlib.suppress(asyncio.QueueFull):
                    self._queue.put_nowait(payload)
        except Exception:
            # notify_pass n-are voie sa arunce spre apelant, orice s-ar intampla.
            log.exception("Eroare neasteptata la pregatirea mesajului Telegram")

    # ------------------------------------------------------------------
    async def _worker(self) -> None:
        """Bucla de trimitere. Nu are voie sa moara: daca task-ul asta cade,
        botul continua sa ruleze dar nu mai trimite notificari, tacut."""
        while not (self._stopped.is_set() and self._queue.empty()):
            try:
                try:
                    payload = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                await self._send_with_retry(payload)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Eroare neasteptata in workerul Telegram")
                await asyncio.sleep(1.0)

    async def _send_with_retry(self, payload: dict[str, Any]) -> None:
        """Trimite un mesaj, cu retry limitat. Nu arunca niciodata mai departe."""
        client = self._client
        if client is None:
            self._failed += 1
            return

        url = f"{TELEGRAM_API}/bot{self._token}/sendMessage"
        backoff = self._backoff_base
        for attempt in range(1, 4):
            try:
                resp = await client.post(url, json=payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Larg intentionat: httpx.HTTPError acopera erorile de retea si
                # de decodare, dar orice alta exceptie neprevazuta trebuie sa
                # duca tot la reincercare si sa fie CONTORIZATA, nu sa piarda
                # mesajul in tacere.
                if not isinstance(exc, httpx.HTTPError):
                    log.warning("Eroare neasteptata la trimiterea catre Telegram: %r", exc)
                if attempt >= 3:
                    log.warning("Nu pot trimite mesajul Telegram dupa %d incercari: %s", attempt, exc)
                    self._failed += 1
                    return
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue

            if resp.status_code == 200:
                self._sent += 1
                return

            if resp.status_code == 429:
                retry_after = self._retry_after(resp)
                if attempt >= 3:
                    log.warning("Telegram rate-limit persistent (429); renunt la mesaj.")
                    self._failed += 1
                    return
                await asyncio.sleep(min(retry_after, 30.0))
                continue

            if resp.status_code >= 500:
                if attempt >= 3:
                    log.warning(
                        "Telegram indisponibil (HTTP %d) dupa %d incercari.", resp.status_code, attempt
                    )
                    self._failed += 1
                    return
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue

            # 4xx altul decat 429: cererea e gresita, retrimiterea n-are rost.
            if resp.status_code == 401:
                log.error("Telegram a refuzat mesajul: token invalid (HTTP 401). Verifica TELEGRAM_BOT_TOKEN.")
            elif resp.status_code == 403:
                log.error(
                    "Telegram a refuzat mesajul: botul nu poate scrie in acest chat (HTTP 403). "
                    "Verifica TELEGRAM_CHAT_ID si ca botul a fost pornit/adaugat in chat."
                )
            elif resp.status_code == 400:
                log.error("Telegram a refuzat mesajul (HTTP 400, cerere invalida - verifica TELEGRAM_CHAT_ID): %s", resp.text[:200])
            else:
                log.error("Telegram a refuzat mesajul (HTTP %d): %s", resp.status_code, resp.text[:200])
            self._failed += 1
            return

        self._failed += 1

    @staticmethod
    def _retry_after(resp: httpx.Response) -> float:
        """Citeste parameters.retry_after din raspuns; 5s implicit daca lipseste."""
        try:
            data = resp.json()
        except ValueError:
            return 5.0
        if not isinstance(data, dict):
            return 5.0
        params = data.get("parameters")
        if isinstance(params, dict):
            value = params.get("retry_after")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
        return 5.0

    # ------------------------------------------------------------------
    @property
    def stats(self) -> dict[str, int]:
        return {
            "sent": self._sent,
            "failed": self._failed,
            "dropped": self._dropped,
            "queued": self._queue.qsize(),
        }
