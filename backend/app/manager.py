"""Managerul de instante: porneste/opreste boti ca procese separate.

De ce procese si nu task-uri asyncio: izolare reala (un bot care crapa nu
doboara serverul), restart individual, si folosirea mai multor nuclee.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bot.config import BotConfig, ConfigError, config_path_for
from bot.logging_setup import get_logger
from bot.settings import BASE_DIR

log = get_logger("app.manager")

# Cat asteptam dupa SIGTERM inainte sa trimitem SIGKILL. Botul are nevoie de
# SHUTDOWN_TASK_TIMEOUT (4s) pentru verificarile in curs plus ~3s ca sa trimita
# ultimele evenimente, deci lasam o marja confortabila.
GRACEFUL_TIMEOUT = 12.0
# Fereastra in care numaram repornirile; peste prag intram in "crash loop".
CRASH_WINDOW = 60.0
MAX_RESTARTS_IN_WINDOW = 5

# Botul iese cu acest cod cand ii lipseste o setare (ex. TELEGRAM_CHAT_ID).
# Repornirea automata n-ar rezolva nimic, doar ar umple logurile.
EXIT_NEEDS_CONFIG = 3


def _signal_group(proc: asyncio.subprocess.Process, sig: int) -> None:
    """Trimite un semnal intregului grup de procese al botului.

    Pe Windows nu exista killpg, iar `start_new_session` e ignorat; acolo
    cadem pe terminate()/kill() direct pe proces.
    """
    killpg = getattr(os, "killpg", None)
    getpgid = getattr(os, "getpgid", None)
    if killpg is not None and getpgid is not None:
        try:
            killpg(getpgid(proc.pid), sig)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass  # grupul nu mai exista sau nu avem drepturi -> fallback mai jos
    with contextlib.suppress(ProcessLookupError, OSError):
        if sig == signal.SIGKILL:
            proc.kill()
        else:
            proc.terminate()


def find_orphan_pids(bot_id: str) -> list[int]:
    """Procese de bot ramase in urma unei opriri necurate a serverului.

    Botii pornesc cu `start_new_session=True`, deci daca serverul e ucis brutal
    (SIGKILL, OOM, cadere de sistem) ei raman in viata si devin invizibili
    pentru urmatoarea instanta de server. Fara verificarea asta, un `start`
    ulterior ar duce la doi boti cu acelasi bot_id, care dubleaza consumul de
    RPC si emit evenimente contradictorii.
    """
    ps = shutil.which("ps")
    if ps is None:  # sisteme fara ps (ex. Windows) - nu putem verifica
        return []
    try:
        out = subprocess.run(
            [ps, "-eo", "pid=,args="], capture_output=True, text=True, timeout=5
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("Nu pot inventaria procesele: %s", exc)
        return []

    pattern = re.compile(rf"bot\.runner\b.*[/\\]{re.escape(bot_id)}\.json\s*$")
    me = os.getpid()
    pids: list[int] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        raw_pid, _, args = line.partition(" ")
        try:
            pid = int(raw_pid)
        except ValueError:
            continue
        if pid != me and pattern.search(args.strip()):
            pids.append(pid)
    return pids


@dataclass
class Instance:
    """Starea unei instante gestionate de server."""

    cfg: BotConfig
    process: asyncio.subprocess.Process | None = None
    orphan_pids: list[int] = field(default_factory=list)
    started_at: float | None = None
    stopped_at: float | None = None
    status: str = "stopped"  # stopped | starting | running | crashed | stopping
    last_error: str = ""
    restarts: int = 0
    autorestart: bool = True
    _restart_times: list[float] = field(default_factory=list)
    _watch_task: asyncio.Task | None = None

    @property
    def pid(self) -> int | None:
        if self.process is not None:
            return self.process.pid
        return self.orphan_pids[0] if self.orphan_pids else None

    @property
    def uptime_s(self) -> float:
        if self.status != "running" or self.started_at is None:
            return 0.0
        return round(time.time() - self.started_at, 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "bot_id": self.cfg.bot_id,
            "label": self.cfg.label,
            "status": self.status,
            "pid": self.pid,
            "uptime_s": self.uptime_s,
            "started_at": self.started_at,
            "stopped_at": self.stopped_at,
            "restarts": self.restarts,
            "autorestart": self.autorestart,
            "orphan_pids": self.orphan_pids,
            "last_error": self.last_error,
            "config": self.cfg.to_dict(),
        }


class BotManager:
    def __init__(self) -> None:
        self._instances: dict[str, Instance] = {}
        self._lock = asyncio.Lock()
        self._shutting_down = False

    # ------------------------------------------------------------------
    def load_from_disk(self) -> None:
        """Inregistreaza (fara sa porneasca) toate configurile gasite."""
        from bot.config import CONFIG_DIR

        if not CONFIG_DIR.exists():
            log.warning("Directorul de configuri lipseste: %s", CONFIG_DIR)
            return
        for path in sorted(CONFIG_DIR.glob("*.json")):
            try:
                cfg = BotConfig.load(path)
            except ConfigError as exc:
                log.error("Config ignorat (%s): %s", path.name, exc)
                continue
            self._instances.setdefault(cfg.bot_id, Instance(cfg=cfg))

        # Reconciliere cu realitatea: pot exista boti ramasi de la o sesiune
        # anterioara incheiata brutal.
        for inst in self._instances.values():
            pids = find_orphan_pids(inst.cfg.bot_id)
            if pids:
                inst.orphan_pids = pids
                inst.status = "orphan"
                inst.last_error = (
                    f"proces ramas din sesiunea anterioara (pid {', '.join(map(str, pids))}). "
                    f"Apasa Stop ca sa-l inchizi, apoi porneste din nou."
                )
                log.error("[%s] %s", inst.cfg.bot_id, inst.last_error)

        log.info("Incarcate %d configuri de bot", len(self._instances))

    def list_instances(self) -> list[dict[str, Any]]:
        return [inst.to_dict() for inst in sorted(self._instances.values(), key=lambda i: i.cfg.bot_id)]

    def get(self, bot_id: str) -> Instance | None:
        return self._instances.get(bot_id)

    # ------------------------------------------------------------------
    async def upsert_config(self, data: dict[str, Any]) -> Instance:
        """Creeaza sau actualizeaza configul unui bot. Nu reporneste automat."""
        cfg = BotConfig.from_dict(data)
        cfg.save(config_path_for(cfg.bot_id))

        async with self._lock:
            inst = self._instances.get(cfg.bot_id)
            if inst is None:
                inst = Instance(cfg=cfg)
                self._instances[cfg.bot_id] = inst
            else:
                inst.cfg = cfg
        return inst

    async def delete(self, bot_id: str) -> bool:
        """Opreste botul si sterge configul de pe disc."""
        inst = self._instances.get(bot_id)
        if inst is None:
            return False
        await self.stop(bot_id)
        async with self._lock:
            self._instances.pop(bot_id, None)
        with contextlib.suppress(OSError, ConfigError):
            config_path_for(bot_id).unlink(missing_ok=True)
        return True

    # ------------------------------------------------------------------
    async def start(self, bot_id: str) -> Instance:
        inst = self._instances.get(bot_id)
        if inst is None:
            raise KeyError(f"nu exista botul '{bot_id}'")

        async with self._lock:
            if inst.process is not None and inst.process.returncode is None:
                return inst  # deja porneste / ruleaza

            if inst.orphan_pids:
                raise RuntimeError(
                    f"'{bot_id}' ruleaza deja ca proces orfan (pid "
                    f"{', '.join(map(str, inst.orphan_pids))}). Opreste-l intai, "
                    f"altfel ai doi boti cu acelasi id pe acelasi RPC."
                )

            config_path = config_path_for(bot_id)
            if not config_path.exists():
                # Configul a fost sters manual - il rescriem din memorie.
                inst.cfg.save(config_path)
            else:
                # Recitim configul de pe disc la fiecare pornire. Altfel, daca
                # editezi fisierul si apesi Restart, botul repornea cu setarile
                # vechi, incarcate la pornirea serverului.
                try:
                    inst.cfg = BotConfig.load(config_path)
                except ConfigError as exc:
                    inst.status = "crashed"
                    inst.last_error = f"config invalid: {exc}"
                    log.error("[%s] %s", bot_id, inst.last_error)
                    raise RuntimeError(inst.last_error) from exc

            inst.status = "starting"
            inst.last_error = ""

            env = os.environ.copy()
            # PYTHONUNBUFFERED: vrem logurile copilului imediat, nu la buffer full.
            env["PYTHONUNBUFFERED"] = "1"

            cmd = [sys.executable, "-m", "bot.runner", "--config", str(config_path)]
            try:
                inst.process = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=str(BASE_DIR),
                    env=env,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                    # Grup de procese propriu: putem opri tot arborele dintr-o data.
                    start_new_session=True,
                )
            except (OSError, ValueError) as exc:
                inst.status = "crashed"
                inst.last_error = f"nu am putut porni procesul: {exc}"
                log.error("[%s] %s", bot_id, inst.last_error)
                raise RuntimeError(inst.last_error) from exc

            inst.started_at = time.time()
            inst.stopped_at = None
            inst.status = "running"
            inst._watch_task = asyncio.create_task(self._watch(inst), name=f"watch-{bot_id}")
            log.info("[%s] Pornit (pid=%s)", bot_id, inst.pid)
        return inst

    # ------------------------------------------------------------------
    async def stop(self, bot_id: str, *, disable_autorestart: bool = True) -> Instance:
        inst = self._instances.get(bot_id)
        if inst is None:
            raise KeyError(f"nu exista botul '{bot_id}'")

        # Acelasi lacat ca in start(): fara el, un stop() concurent putea citi
        # inst.process inainte ca start() sa apuce sa-l atribuie, marca instanta
        # "stopped" si dezactiva autorestart-ul, in timp ce procesul chiar rula.
        async with self._lock:
            if disable_autorestart:
                inst.autorestart = False

            proc = inst.process
            if proc is None or proc.returncode is not None:
                if inst.orphan_pids:
                    self._stop_orphans(inst)
                inst.status = "stopped"
                return inst

            inst.status = "stopping"
            log.info("[%s] Trimit SIGTERM catre pid=%s", bot_id, proc.pid)
            _signal_group(proc, signal.SIGTERM)

        # Asteptarea se face IN AFARA lacatului: altfel un bot lent la oprire ar
        # bloca orice operatie pe celelalte instante.
        try:
            await asyncio.wait_for(proc.wait(), timeout=GRACEFUL_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning("[%s] Nu s-a oprit in %.0fs, trimit SIGKILL", bot_id, GRACEFUL_TIMEOUT)
            _signal_group(proc, signal.SIGKILL)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=3.0)

        async with self._lock:
            inst.status = "stopped"
            inst.stopped_at = time.time()
        return inst

    def _stop_orphans(self, inst: Instance) -> None:
        """Inchide procesele ramase din sesiunea anterioara pentru acest bot_id."""
        for pid in list(inst.orphan_pids):
            try:
                os.kill(pid, signal.SIGTERM)
                log.info("[%s] SIGTERM catre procesul orfan pid=%s", inst.cfg.bot_id, pid)
            except ProcessLookupError:
                pass  # deja mort
            except PermissionError as exc:
                log.error("[%s] Nu pot opri pid=%s: %s", inst.cfg.bot_id, pid, exc)
                continue
            inst.orphan_pids.remove(pid)
        if not inst.orphan_pids:
            inst.last_error = ""

    async def restart(self, bot_id: str) -> Instance:
        await self.stop(bot_id)
        inst = self._instances.get(bot_id)
        if inst is not None:
            inst.autorestart = True
        return await self.start(bot_id)

    # ------------------------------------------------------------------
    async def _watch(self, inst: Instance) -> None:
        """Asteapta terminarea procesului si decide daca il repornim."""
        proc = inst.process
        if proc is None:
            return

        stderr_tail = ""
        try:
            if proc.stderr is not None:
                stderr_tail = await self._drain_stderr(inst, proc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Citirea stderr-ului nu are voie sa ne faca sa pierdem urma
            # procesului: fara proc.wait() de mai jos, instanta ar ramane
            # raportata "running" la nesfarsit, fara autorestart.
            log.error("[%s] Nu am putut citi stderr-ul: %s", inst.cfg.bot_id, exc)

        try:
            code = await proc.wait()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("[%s] Eroare la asteptarea procesului: %s", inst.cfg.bot_id, exc)
            inst.status = "crashed"
            inst.last_error = f"watcher: {exc}"
            return

        inst.stopped_at = time.time()

        if inst.status == "stopping" or self._shutting_down:
            inst.status = "stopped"
            log.info("[%s] Oprit curat (cod %s)", inst.cfg.bot_id, code)
            return

        if code == 0:
            inst.status = "stopped"
            log.info("[%s] Procesul s-a incheiat singur (cod 0)", inst.cfg.bot_id)
            return

        if code == EXIT_NEEDS_CONFIG:
            inst.status = "stopped"
            inst.autorestart = False
            inst.last_error = (
                "configuratie incompleta - vezi logul botului. "
                f"{stderr_tail}".strip()
            )
            log.error(
                "[%s] Oprit: configuratie incompleta. Nu repornesc automat.",
                inst.cfg.bot_id,
            )
            return

        inst.status = "crashed"
        inst.last_error = f"proces terminat cu cod {code}. {stderr_tail}".strip()
        log.error("[%s] Crash (cod %s): %s", inst.cfg.bot_id, code, stderr_tail[:400])

        if not inst.autorestart:
            return

        # Protectie anti crash-loop: max N reporniri intr-o fereastra de timp.
        now = time.time()
        inst._restart_times = [t for t in inst._restart_times if now - t < CRASH_WINDOW]
        if len(inst._restart_times) >= MAX_RESTARTS_IN_WINDOW:
            inst.last_error += " | prea multe reporniri, autorestart dezactivat"
            inst.autorestart = False
            log.error("[%s] Crash loop detectat, nu mai repornesc automat", inst.cfg.bot_id)
            return

        inst._restart_times.append(now)
        inst.restarts += 1
        delay = min(2 ** len(inst._restart_times), 30)
        log.info("[%s] Repornire automata in %ds", inst.cfg.bot_id, delay)
        await asyncio.sleep(delay)
        if not self._shutting_down and inst.autorestart:
            with contextlib.suppress(Exception):
                await self.start(inst.cfg.bot_id)

    async def _drain_stderr(self, inst: Instance, proc: asyncio.subprocess.Process) -> str:
        """Citeste stderr-ul copilului, il logheaza si pastreaza ultimele linii."""
        tail: list[str] = []
        assert proc.stderr is not None
        try:
            while True:
                try:
                    # readline() nu arunca pe linii peste limita de buffer -
                    # spre deosebire de `async for`, care ridica ValueError.
                    raw = await proc.stderr.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    log.warning("[%s] Linie de stderr peste limita, o sar", inst.cfg.bot_id)
                    continue
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip()
                if not line:
                    continue
                # Copilul are deja timestamp + nivel; il redam ca atare.
                print(f"  ├─ {inst.cfg.bot_id} │ {line}", file=sys.stderr, flush=True)
                tail.append(line)
                if len(tail) > 20:
                    tail.pop(0)
        except (asyncio.CancelledError, GeneratorExit):
            raise
        except Exception as exc:
            log.debug("[%s] Citire stderr intrerupta: %s", inst.cfg.bot_id, exc)
        return " | ".join(tail[-3:])

    # ------------------------------------------------------------------
    async def shutdown_all(self) -> None:
        """Opreste toti botii la inchiderea serverului."""
        self._shutting_down = True
        running = [i.cfg.bot_id for i in self._instances.values() if i.status in ("running", "starting")]
        if not running:
            return
        log.info("Opresc %d boti...", len(running))
        await asyncio.gather(*(self.stop(bid) for bid in running), return_exceptions=True)
        for inst in self._instances.values():
            if inst._watch_task is not None:
                inst._watch_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await inst._watch_task
