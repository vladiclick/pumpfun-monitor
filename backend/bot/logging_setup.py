"""Logging structurat, cu prefix de bot_id, folosit peste tot in proiect."""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_CONFIGURED = False

# Format compact dar complet: timp, nivel, sursa, mesaj.
_FMT = "%(asctime)s | %(levelname)-7s | %(name)-22s | %(message)s"
_DATEFMT = "%H:%M:%S"


class _ColorFormatter(logging.Formatter):
    """Colorare minimala pe terminal; fara culori daca iesirea nu e TTY."""

    COLORS = {
        "DEBUG": "\033[90m",
        "INFO": "\033[36m",
        "WARNING": "\033[33m",
        "ERROR": "\033[31m",
        "CRITICAL": "\033[41m",
    }
    RESET = "\033[0m"

    def __init__(self, use_color: bool) -> None:
        super().__init__(_FMT, datefmt=_DATEFMT)
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        if not self.use_color:
            return text
        color = self.COLORS.get(record.levelname, "")
        return f"{color}{text}{self.RESET}" if color else text


def setup_logging(level: str = "INFO", log_file: Path | None = None) -> None:
    """Configureaza root logger-ul o singura data per proces."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(_ColorFormatter(use_color=sys.stderr.isatty()))
    root.addHandler(stream)

    if log_file is not None:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            # 5 fisiere x 2 MB - suficient pentru rulare pe termen lung.
            fh = RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
            fh.setFormatter(logging.Formatter(_FMT, datefmt="%Y-%m-%d %H:%M:%S"))
            root.addHandler(fh)
        except OSError as exc:  # disc plin / permisiuni - nu oprim botul pentru asta
            root.warning("Nu pot scrie fisierul de log %s: %s", log_file, exc)

    # Bibliotecile terte sunt zgomotoase pe DEBUG; le tinem pe WARNING.
    for noisy in ("websockets.client", "websockets.protocol", "httpx", "httpcore", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
