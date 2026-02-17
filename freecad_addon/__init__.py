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


def _reload_submodules() -> None:
    """Reload all addon submodules so code changes take effect without restarting FreeCAD."""
    import importlib
    import sys

    submodules = [
        "freecad_addon.compat",
        "freecad_addon.commands",
        "freecad_addon.protocol",
        "freecad_addon.selection_observer",
        "freecad_addon.socket_server",
    ]
    for name in submodules:
        if name in sys.modules:
            importlib.reload(sys.modules[name])
            logger.info("Reloaded %s", name)


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


def _run_health_check() -> None:
    """Log FreeCAD version and probe critical modules at startup."""
    try:
        from freecad_addon.compat import IS_V1_PLUS, VERSION_TUPLE, probe_modules
        logger.info(
            "FreeCAD version: %d.%d (v1+: %s)",
            VERSION_TUPLE[0], VERSION_TUPLE[1], IS_V1_PLUS,
        )
        modules = probe_modules()
        for mod, available in modules.items():
            status = "[OK]" if available else "[MISSING]"
            logger.info("  Module %-20s %s", mod, status)
        if not modules.get("JointObject", False) or not modules.get("UtilsAssembly", False):
            logger.warning(
                "Assembly workbench modules not available — "
                "Tier 2 motion features (create_assembly, drive_joint, check_interference) won't work. "
                "Tier 1 analytical validation still works."
            )
    except Exception as exc:
        logger.warning("Health check failed: %s", exc)


def start(host: str = "127.0.0.1", port: int = 9876) -> None:
    """Start the SolidMind addon (socket server + selection observer)."""
    global _started
    if _started:
        logger.info("SolidMind addon already started")
        return

    session_dir = _setup_session_logging()
    logger.info("Session log directory: %s", session_dir)

    # Reload submodules to pick up code changes (dev hot-reload)
    _reload_submodules()

    # Run health check to log version and module availability
    _run_health_check()

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


def restart(host: str = "127.0.0.1", port: int = 9876) -> None:
    """Stop, reload all submodules, and start again.

    Convenience wrapper for the common dev workflow of picking up code
    changes without restarting FreeCAD.  Equivalent to::

        freecad_addon.stop()
        freecad_addon.start()

    but also reloads *this* ``__init__`` module so that changes to
    ``start()``/``stop()`` themselves take effect.
    """
    import importlib
    import sys

    stop()

    # Reload all submodules + this package so every layer picks up changes
    for name in [
        "freecad_addon.compat",
        "freecad_addon.commands",
        "freecad_addon.protocol",
        "freecad_addon.selection_observer",
        "freecad_addon.socket_server",
        "freecad_addon",
    ]:
        if name in sys.modules:
            importlib.reload(sys.modules[name])
            logger.info("Reloaded %s", name)

    # After reloading freecad_addon, call the *new* start()
    sys.modules["freecad_addon"].start(host=host, port=port)
