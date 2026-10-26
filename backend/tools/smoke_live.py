"""Test live (necesita internet): asculta pump.fun cateva secunde si valideaza fluxul.

Rulare:
    .venv/bin/python -m tools.smoke_live --seconds 45 --max-tokens 5

Nu are nevoie de backend pornit: emitentul e inlocuit cu un colector local.
"""
from __future__ import annotations

import argparse
import asyncio
import time

import httpx

from bot.checks import AntiRugPipeline
from bot.config import BotConfig
from bot.listener import PumpFunListener
from bot.logging_setup import setup_logging
from bot.parser import parse_create_logs, parse_create_transaction
from bot.rpc import SolanaRpc
from bot.settings import SETTINGS


async def run(seconds: float, max_tokens: int, full_pipeline: bool) -> int:
    setup_logging("INFO")
    cfg = BotConfig(
        bot_id="smoke",
        check_liquidity=full_pipeline,
        check_rugcheck=full_pipeline,
        max_concurrent_checks=32,
        liquidity_wait_seconds=6,
        liquidity_poll_interval=3,
        min_liquidity_usd=1,
        allow_missing_liquidity=True,
    )
    cfg.validate()

    rpc = SolanaRpc(SETTINGS.rpc_http, timeout=12, min_interval=0.35, max_retries=3)
    http = httpx.AsyncClient(timeout=12, follow_redirects=True)
    pipeline = AntiRugPipeline(cfg, rpc, http)
    listener = PumpFunListener(cfg, SETTINGS.rpc_ws)

    print(f"RPC HTTP : {SETTINGS.rpc_http}")
    print(f"RPC WS   : {SETTINGS.rpc_ws}")
    print(f"Health   : {await rpc.get_health()}")
    print(f"Ascult {seconds:.0f}s, maxim {max_tokens} token-uri...\n")

    detected = resolved = 0
    deadline = time.monotonic() + seconds
    rows: list[str] = []

    async def consume() -> None:
        nonlocal detected, resolved
        async for detection in listener.detections():
            detected += 1
            signature = detection.signature
            t0 = detection.detected_at

            # Calea rapida: din loguri, fara RPC.
            info = parse_create_logs(detection.logs)
            if info is None:
                tx = None
                for _ in range(3):
                    tx = await rpc.get_transaction(signature)
                    if tx is not None:
                        break
                    await asyncio.sleep(0.6)
                info = parse_create_transaction(tx)

            if info is None:
                print(f"  [!] {signature[:12]}... nu s-a putut parsa")
                continue
            resolved += 1
            res = await pipeline.run(info.mint)
            ms = int((time.monotonic() - t0) * 1000)
            rows.append(
                f"  {res.verdict:<7} {(info.symbol or '?')[:10]:<10} "
                f"{info.mint[:6]}..{info.mint[-4:]}  {ms:>5}ms  "
                f"[{info.source}] {res.reason[:60]}"
            )
            print(rows[-1])
            if resolved >= max_tokens:
                listener.stop()
                return

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=max(1.0, deadline - time.monotonic()))
    except asyncio.TimeoutError:
        listener.stop()
        try:
            await asyncio.wait_for(task, timeout=15)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.cancel()
    finally:
        await rpc.aclose()
        await http.aclose()

    print(f"\nRezultat: {detected} detectate, {resolved} rezolvate complet, "
          f"{listener.logs_received} mesaje WS, {listener.reconnects} reconectari")
    return 0 if resolved > 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Smoke test live pe pump.fun")
    ap.add_argument("--seconds", type=float, default=45)
    ap.add_argument("--max-tokens", type=int, default=5)
    ap.add_argument("--full", action="store_true", help="ruleaza si DexScreener + RugCheck")
    args = ap.parse_args()
    return asyncio.run(run(args.seconds, args.max_tokens, args.full))


if __name__ == "__main__":
    raise SystemExit(main())
