"""SolidMind CAD — FreeCAD addon.

When loaded inside FreeCAD, this package starts a TCP socket server that
accepts commands from the MCP bridge process and drives FreeCAD's PartDesign
workbench.

Usage inside FreeCAD's Python console or startup macro::

    import freecad_addon
    freecad_addon.start()

Or to auto-start, add to FreeCAD's macro startup.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("solidmind")

_started = False


def _setup_session_logging() -> Path:
    """Configure file logging into a per-session subdirectory.

    Creates ``~/.solidmind/logs/<YYYYMMDD-HHMMSS>/session.log`` and attaches
    a ``FileHandler`` to the ``solidmind`` root logger.  Returns the session
    directory path.
    """
    log_root = Path(os.environ.get("SOLIDMIND_LOG_DIR", Path.home() / ".solidmind" / "logs"))
    session_dir = log_root / datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
    session_dir.mkdir(parents=True, exist_ok=True)

    log_file = session_dir / "session.log"
    handler = logging.FileHandler(log_file, encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    ))

    root_logger = logging.getLogger("solidmind")
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.DEBUG)

    return session_dir


def start(host: str = "127.0.0.1", port: int = 9876) -> None:
    """Start the SolidMind addon (socket server + selection observer)."""
    global _started
    if _started:
        logger.info("SolidMind addon already started")
        return

    session_dir = _setup_session_logging()
    logger.info("Session log directory: %s", session_dir)

    from freecad_addon.selection_observer import get_observer
    from freecad_addon.socket_server import start_server

    observer = get_observer()
    observer.start()
    logger.info("Selection observer started")

    start_server(host=host, port=port)
    _started = True
    logger.info("SolidMind addon ready (listening on %s:%d)", host, port)


def stop() -> None:
    """Stop the SolidMind addon."""
    global _started

    from freecad_addon.selection_observer import get_observer
    from freecad_addon.socket_server import stop_server

    stop_server()
    observer = get_observer()
    observer.stop()
    _started = False
    logger.info("SolidMind addon stopped")
