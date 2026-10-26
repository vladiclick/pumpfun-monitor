"""Serverul FastAPI: ingest de la boti, WebSocket live si REST pentru dashboard."""
from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from bot.config import ConfigError
from bot.logging_setup import get_logger, setup_logging
from bot.settings import BASE_DIR, SETTINGS, mask_endpoint

from .db import Database
from .hub import Hub
from .manager import BotManager
from .models import ActionResponse, BotConfigPayload, IngestPayload

setup_logging(SETTINGS.log_level, log_file=BASE_DIR / "data" / "logs" / "server.log")
log = get_logger("app.main")

START_TIME = time.time()
PRUNE_INTERVAL_S = 6 * 3600
KEEP_DAYS = 14
# Dashboardul trimite "ping" la 20s. Daca nu primim nimic mult peste asta,
# conexiunea e moarta (laptop adormit, NAT care taie sesiunea) si o inchidem -
# altfel task-urile si cozile ei raman alocate la nesfarsit.
WS_IDLE_TIMEOUT_S = 75.0

db = Database(SETTINGS.db_path)
hub = Hub()
manager = BotManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Pornire/oprire controlata a resurselor."""
    await db.connect()
    manager.load_from_disk()
    prune_task = asyncio.create_task(_prune_loop(), name="prune-loop")
    log.info("Server pornit. RPC=%s", mask_endpoint(SETTINGS.rpc_http))
    try:
        yield
    finally:
        prune_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await prune_task
        await manager.shutdown_all()
        await db.close()
        log.info("Server oprit curat.")


async def _prune_loop() -> None:
    """Curata periodic evenimentele vechi ca fisierul SQLite sa nu creasca la infinit."""
    while True:
        try:
            await asyncio.sleep(PRUNE_INTERVAL_S)
            await db.prune(KEEP_DAYS)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("Prune loop: %s", exc)


app = FastAPI(
    title="pump.fun monitor",
    version="1.0.0",
    description="Backend pentru monitorizarea lansarilor de token-uri pe pump.fun",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=SETTINGS.cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ----------------------------------------------------------------------
# Autentificare simpla pentru ingest (secret partajat din .env)
# ----------------------------------------------------------------------
async def require_ingest_token(x_ingest_token: str = Header(default="")) -> None:
    if not SETTINGS.ingest_token:
        return  # dezactivat explicit (token gol in .env)
    # compare_digest evita scurgerea de informatie prin timpul de comparatie.
    if not secrets.compare_digest(x_ingest_token, SETTINGS.ingest_token):
        raise HTTPException(status_code=401, detail="token de ingest invalid")


def _dashboard_token_ok(supplied: str) -> bool:
    if not SETTINGS.dashboard_token:
        return True  # dashboard deschis (implicit, pentru rulare locala)
    return secrets.compare_digest(supplied or "", SETTINGS.dashboard_token)


async def require_dashboard_token(
    x_dashboard_token: str = Header(default=""),
    token: str = Query(default=""),
) -> None:
    """Protejeaza REST-ul si WebSocket-ul cand DASHBOARD_TOKEN e setat in .env.

    Gol = comportamentul implicit de pana acum (deschis, pentru localhost).
    Setat = obligatoriu inainte sa expui dashboardul prin Cloudflare Tunnel:
    altfel oricine are linkul poate porni si opri boti.
    """
    if not _dashboard_token_ok(x_dashboard_token or token):
        raise HTTPException(status_code=401, detail="token de dashboard invalid")


# ----------------------------------------------------------------------
# Ingest de la boti
# ----------------------------------------------------------------------
@app.post("/api/ingest", dependencies=[Depends(require_ingest_token)])
async def ingest(payload: IngestPayload) -> dict[str, Any]:
    """Primeste un lot de evenimente, il salveaza si il difuzeaza pe WebSocket."""
    events = [e.model_dump() for e in payload.events]

    # Difuzam intai (live-ul e prioritar), scriem pe disc imediat dupa.
    await hub.broadcast(events)
    stored = await db.insert_events(events)
    return {"ok": True, "received": len(events), "stored": stored}


# ----------------------------------------------------------------------
# WebSocket pentru dashboard
# ----------------------------------------------------------------------
@app.websocket("/ws")
async def dashboard_ws(ws: WebSocket, token: str = Query(default="")) -> None:
    supplied = ws.headers.get("x-dashboard-token") or token
    if not _dashboard_token_ok(supplied):
        # 1008 = Policy Violation. Inchidem inainte de accept.
        await ws.close(code=1008, reason="token de dashboard invalid")
        return

    await ws.accept()
    client = await hub.register(ws)
    pump = asyncio.create_task(hub.pump(client))

    try:
        # Snapshot initial: instante + ultimele evenimente, ca UI-ul sa nu fie gol.
        await hub.send_json(client, {
            "type": "snapshot",
            "instances": await _instances_with_stats(),
            "events": hub.replay_buffer() or await db.query_events(limit=100),
            "server": _server_info(),
        })

        while True:
            # Clientul trimite doar ping-uri sau cereri de refresh.
            try:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=WS_IDLE_TIMEOUT_S)
            except asyncio.TimeoutError:
                log.info("Dashboard inactiv %.0fs - inchid conexiunea", WS_IDLE_TIMEOUT_S)
                with contextlib.suppress(Exception):
                    await ws.close(code=1001, reason="inactiv")
                break
            if raw == "ping":
                await hub.send_json(client, {"type": "pong", "ts": int(time.time() * 1000)})
            elif raw == "refresh":
                await hub.send_json(client, {
                    "type": "instances",
                    "instances": await _instances_with_stats(),
                })
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.debug("WS dashboard inchis: %s", exc)
    finally:
        pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pump
        await hub.unregister(client)


# ----------------------------------------------------------------------
# REST: instante
# ----------------------------------------------------------------------
async def _instances_with_stats() -> list[dict[str, Any]]:
    """Lista de instante imbogatita cu agregatele din SQLite.

    Contoarele in-memory ale botilor dispar la restart, iar dashboardul are
    nevoie de cifre stabile. Folosim aceeasi forma peste tot (REST, snapshot
    WebSocket, push dupa o actiune) - altfel panoul afisa 0 dupa start/stop,
    pana la urmatorul poll.
    """
    instances = manager.list_instances()
    for inst in instances:
        inst["stats"] = await db.bot_stats(inst["bot_id"])
    return instances


@app.get("/api/instances", dependencies=[Depends(require_dashboard_token)])
async def list_instances() -> dict[str, Any]:
    return {"instances": await _instances_with_stats(), "server": _server_info()}


@app.post("/api/instances", response_model=ActionResponse, dependencies=[Depends(require_dashboard_token)])
async def create_instance(payload: BotConfigPayload) -> ActionResponse:
    try:
        inst = await manager.upsert_config(payload.as_dict())
    except ConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await _push_instances()
    return ActionResponse(message=f"config salvat pentru '{inst.cfg.bot_id}'", instance=inst.to_dict())


@app.post("/api/instances/{bot_id}/start", response_model=ActionResponse, dependencies=[Depends(require_dashboard_token)])
async def start_instance(bot_id: str) -> ActionResponse:
    try:
        inst = await manager.start(bot_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    await _push_instances()
    return ActionResponse(message=f"'{bot_id}' pornit", instance=inst.to_dict())


@app.post("/api/instances/{bot_id}/stop", response_model=ActionResponse, dependencies=[Depends(require_dashboard_token)])
async def stop_instance(bot_id: str) -> ActionResponse:
    try:
        inst = await manager.stop(bot_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await _push_instances()
    return ActionResponse(message=f"'{bot_id}' oprit", instance=inst.to_dict())


@app.post("/api/instances/{bot_id}/restart", response_model=ActionResponse, dependencies=[Depends(require_dashboard_token)])
async def restart_instance(bot_id: str) -> ActionResponse:
    try:
        inst = await manager.restart(bot_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    await _push_instances()
    return ActionResponse(message=f"'{bot_id}' repornit", instance=inst.to_dict())


@app.delete("/api/instances/{bot_id}", response_model=ActionResponse, dependencies=[Depends(require_dashboard_token)])
async def delete_instance(bot_id: str) -> ActionResponse:
    ok = await manager.delete(bot_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"nu exista botul '{bot_id}'")
    await _push_instances()
    return ActionResponse(message=f"'{bot_id}' sters")


# ----------------------------------------------------------------------
# REST: istoric si statistici
# ----------------------------------------------------------------------
@app.get("/api/events", dependencies=[Depends(require_dashboard_token)])
async def get_events(
    bot_id: str | None = None,
    type: str | None = Query(default=None, description="filtru dupa tip, separat prin virgula"),
    verdict: str | None = None,
    search: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    before_ts: int | None = None,
) -> dict[str, Any]:
    events = await db.query_events(
        bot_id=bot_id, event_type=type, verdict=verdict,
        search=search, limit=limit, before_ts=before_ts,
    )
    return {"events": events, "count": len(events)}


@app.get("/api/stats", dependencies=[Depends(require_dashboard_token)])
async def get_stats(bot_id: str | None = None) -> dict[str, Any]:
    return {"stats": await db.bot_stats(bot_id), "server": _server_info()}


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "uptime_s": round(time.time() - START_TIME, 1),
        "instances": len(manager.list_instances()),
        "dashboards": hub.client_count,
    }


# ----------------------------------------------------------------------
def _server_info() -> dict[str, Any]:
    return {
        "uptime_s": round(time.time() - START_TIME, 1),
        "dashboards": hub.client_count,
        "rpc_http": mask_endpoint(SETTINGS.rpc_http),
        "rpc_ws": mask_endpoint(SETTINGS.rpc_ws),
    }


async def _push_instances() -> None:
    """Notifica dashboardurile ca lista de instante s-a schimbat."""
    await hub.broadcast_json(
        {"type": "instances", "instances": await _instances_with_stats()}
    )


@app.exception_handler(ConfigError)
async def config_error_handler(_request, exc: ConfigError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": str(exc)})


# Servim build-ul de frontend daca exista (util cand expui public un singur port).
_FRONTEND_DIST = BASE_DIR.parent / "frontend" / "dist"
if _FRONTEND_DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIST), html=True), name="frontend")
    log.info("Servesc frontend-ul din %s", _FRONTEND_DIST)
