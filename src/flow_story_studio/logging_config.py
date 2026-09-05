"""Persistent application logging for desktop production builds."""

from __future__ import annotations

import logging
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOGGER_NAME = "flow_story_studio"
_CONFIGURED_ROOTS: set[Path] = set()


def configure_logging(log_root: Path, *, level: int = logging.INFO) -> Path:
    """Configure rotating UTF-8 file logs once per root and return the log path."""
    root = log_root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    log_path = root / "studio.log"
    if root in _CONFIGURED_ROOTS:
        return log_path

    handler = RotatingFileHandler(
        log_path,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s [%(threadName)s] %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )

    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(level)
    logger.addHandler(handler)
    logger.propagate = False
    _CONFIGURED_ROOTS.add(root)

    def log_unhandled(exc_type, exc_value, exc_traceback) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        logger.critical("Unhandled exception", exc_info=(exc_type, exc_value, exc_traceback))

    sys.excepthook = log_unhandled

    original_thread_hook = threading.excepthook

    def log_thread_unhandled(args: threading.ExceptHookArgs) -> None:
        logger.critical(
            "Unhandled thread exception in %s",
            args.thread.name if args.thread else "unknown",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )
        if args.exc_type is KeyboardInterrupt:
            original_thread_hook(args)

    threading.excepthook = log_thread_unhandled
    logger.info("Persistent logging initialized: %s", log_path)
    return log_path


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"{_LOGGER_NAME}.{name}")
