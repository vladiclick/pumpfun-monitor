"""Stocare SQLite pentru istoricul evenimentelor.

Folosim aiosqlite ca sa nu blocam bucla asyncio a FastAPI. Modul WAL permite
citiri concurente in timp ce botii scriu.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterable

import aiosqlite

from bot.logging_setup import get_logger

log = get_logger("app.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id     TEXT    NOT NULL UNIQUE,
    bot_id       TEXT    NOT NULL,
    type         TEXT    NOT NULL,
    ts           INTEGER NOT NULL,
    received_at  INTEGER NOT NULL,
    signature    TEXT,
    mint         TEXT,
    name         TEXT,
    symbol       TEXT,
    verdict      TEXT,
    reason       TEXT,
    failed_stage TEXT,
    latency_ms   INTEGER,
    level        TEXT,
    message      TEXT,
    payload      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts      ON events(ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_bot_ts  ON events(bot_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_type    ON events(type, ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_mint    ON events(mint);
"""

# Coloanele extrase din payload in coloane proprii (pentru filtrare rapida).
_COLUMNS = (
    "event_id", "bot_id", "type", "ts", "received_at", "signature", "mint",
    "name", "symbol", "verdict", "reason", "failed_stage", "latency_ms",
    "level", "message", "payload",
)


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    # ------------------------------------------------------------------
    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        # WAL + synchronous=NORMAL: scrieri rapide, fara risc de corupere la crash.
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()
        log.info("SQLite deschis: %s", self.path)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("baza de date nu e conectata")
        return self._conn

    # ------------------------------------------------------------------
    async def insert_events(self, events: Iterable[dict[str, Any]]) -> int:
        """Insereaza un lot. Duplicatele (acelasi event_id) sunt ignorate."""
        now = int(time.time() * 1000)
        rows = []
        for ev in events:
            rows.append(
                (
                    ev.get("event_id"),
                    ev.get("bot_id"),
                    ev.get("type"),
                    int(ev.get("ts") or now),
                    now,
                    ev.get("signature"),
                    ev.get("mint"),
                    ev.get("name"),
                    ev.get("symbol"),
                    ev.get("verdict"),
                    ev.get("reason"),
                    ev.get("failed_stage"),
                    ev.get("latency_ms"),
                    ev.get("level"),
                    ev.get("message"),
                    json.dumps(ev, ensure_ascii=False),
                )
            )
        if not rows:
            return 0

        placeholders = ",".join("?" * len(_COLUMNS))
        sql = f"INSERT OR IGNORE INTO events ({','.join(_COLUMNS)}) VALUES ({placeholders})"
        try:
            cursor = await self.conn.executemany(sql, rows)
            await self.conn.commit()
            return cursor.rowcount or 0
        except aiosqlite.Error as exc:
            log.error("Nu am putut scrie evenimentele in SQLite: %s", exc)
            # Nu propagam: pierderea istoricului nu trebuie sa opreasca live feed-ul.
            return 0

    # ------------------------------------------------------------------
    async def query_events(
        self,
        *,
        bot_id: str | None = None,
        event_type: str | None = None,
        verdict: str | None = None,
        search: str | None = None,
        limit: int = 100,
        before_ts: int | None = None,
    ) -> list[dict[str, Any]]:
        """Istoric filtrat, cel mai nou primul."""
        clauses: list[str] = []
        params: list[Any] = []

        if bot_id:
            clauses.append("bot_id = ?")
            params.append(bot_id)
        if event_type:
            types = [t.strip() for t in event_type.split(",") if t.strip()]
            clauses.append(f"type IN ({','.join('?' * len(types))})")
            params.extend(types)
        if verdict:
            clauses.append("verdict = ?")
            params.append(verdict.upper())
        if search:
            clauses.append("(mint LIKE ? OR name LIKE ? OR symbol LIKE ? OR signature LIKE ?)")
            like = f"%{search}%"
            params.extend([like] * 4)
        if before_ts:
            clauses.append("ts < ?")
            params.append(before_ts)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        limit = max(1, min(limit, 1000))
        sql = f"SELECT payload FROM events {where} ORDER BY ts DESC, id DESC LIMIT ?"
        params.append(limit)

        try:
            async with self.conn.execute(sql, params) as cur:
                rows = await cur.fetchall()
        except aiosqlite.Error as exc:
            log.error("Interogare esuata: %s", exc)
            return []

        out = []
        for row in rows:
            try:
                out.append(json.loads(row["payload"]))
            except (ValueError, TypeError):
                continue
        return out

    # ------------------------------------------------------------------
    async def bot_stats(self, bot_id: str | None = None) -> dict[str, Any]:
        """Agregate pentru dashboard (total scanate / trecute / latenta medie)."""
        where = "WHERE bot_id = ?" if bot_id else ""
        params = [bot_id] if bot_id else []
        sql = f"""
            SELECT
              SUM(type = 'token_detected')                    AS detected,
              SUM(type = 'verdict')                           AS verdicts,
              SUM(verdict = 'PASS')                           AS passed,
              SUM(verdict = 'FAIL')                           AS failed,
              SUM(verdict = 'PENDING')                        AS pending,
              SUM(verdict = 'ERROR')                          AS errors,
              AVG(CASE WHEN latency_ms > 0 THEN latency_ms END) AS avg_latency,
              MIN(CASE WHEN latency_ms > 0 THEN latency_ms END) AS min_latency,
              MAX(latency_ms)                                 AS max_latency
            FROM events {where}
        """
        try:
            async with self.conn.execute(sql, params) as cur:
                row = await cur.fetchone()
        except aiosqlite.Error as exc:
            log.error("Agregare esuata: %s", exc)
            return {}

        if row is None:
            return {}
        return {
            "detected": row["detected"] or 0,
            "verdicts": row["verdicts"] or 0,
            "passed": row["passed"] or 0,
            "failed": row["failed"] or 0,
            "pending": row["pending"] or 0,
            "errors": row["errors"] or 0,
            "avg_latency_ms": round(row["avg_latency"]) if row["avg_latency"] else None,
            "min_latency_ms": row["min_latency"],
            "max_latency_ms": row["max_latency"],
        }

    async def prune(self, keep_days: int = 14) -> int:
        """Sterge evenimentele mai vechi de N zile (rulare pe termen lung)."""
        cutoff = int((time.time() - keep_days * 86400) * 1000)
        try:
            cur = await self.conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            await self.conn.commit()
            deleted = cur.rowcount or 0
            if deleted:
                log.info("Curatate %d evenimente mai vechi de %d zile", deleted, keep_days)
            return deleted
        except aiosqlite.Error as exc:
            log.error("Prune esuat: %s", exc)
            return 0
