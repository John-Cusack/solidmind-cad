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

logger = logging.getLogger("solidmind")

_started = False


def start(host: str = "127.0.0.1", port: int = 9876) -> None:
    """Start the SolidMind addon (socket server + selection observer)."""
    global _started
    if _started:
        logger.info("SolidMind addon already started")
        return

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
