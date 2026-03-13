#!/usr/bin/env python3
"""CLI tool for Isaac Sim lifecycle management.

Wraps ``IsaacLifecycle`` with argparse for interactive and scripted use.

Examples::

    # Start non-headless, import URDF, take screenshot, stay alive for inspection
    ISAAC_PYTHON=../isaacsim/_build/.../python.sh \\
        python3 scripts/isaac_lifecycle.py \\
        --no-headless --urdf hexapod_sim_pkg/Hexapod_v2_1DOF.urdf --screenshot

    # Start, import, screenshot to file, exit
    python3 scripts/isaac_lifecycle.py \\
        --urdf path/to.urdf --screenshot --screenshot-path out.png --exit-after

    # Just start bridge on a port and wait
    python3 scripts/isaac_lifecycle.py --no-headless --port 9878

The main thread runs ``_pump_main_thread()`` (required for Kit event loop).
All operations go through the TCP client which dispatches to main thread
via ``MainThreadDispatcher``.
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Isaac Sim lifecycle manager — start, import, screenshot, inspect",
    )
    parser.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Run in headless mode (default: true, use --no-headless for GUI)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bridge bind host (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="Bridge bind port (default: 0 = ephemeral)",
    )
    parser.add_argument(
        "--urdf",
        type=str,
        default=None,
        help="URDF file to import after startup",
    )
    parser.add_argument(
        "--screenshot",
        action="store_true",
        help="Take a screenshot after import",
    )
    parser.add_argument(
        "--screenshot-path",
        type=str,
        default=None,
        help="Save screenshot to this path (default: isaac_lifecycle_screenshot.png)",
    )
    parser.add_argument(
        "--exit-after",
        action="store_true",
        help="Exit after completing operations (default: stay alive for inspection)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="Bridge startup timeout in seconds (default: 60)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO)",
    )

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
    )
    logger = logging.getLogger("solidmind.isaac_lifecycle_cli")

    # Late import to avoid requiring Isaac at CLI parse time
    from isaac_bridge.lifecycle import IsaacLifecycle

    lifecycle = IsaacLifecycle(
        headless=args.headless,
        host=args.host,
        port=args.port,
    )

    # Handle Ctrl+C gracefully
    shutdown_requested = False

    def _signal_handler(_signum: int, _frame: object) -> None:
        nonlocal shutdown_requested
        if shutdown_requested:
            logger.warning("Force exit")
            sys.exit(1)
        shutdown_requested = True
        logger.info("Shutdown requested (Ctrl+C again to force)")

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    try:
        logger.info("Starting Isaac lifecycle (headless=%s, port=%d)...", args.headless, args.port)
        lifecycle.start(timeout=args.timeout)
        logger.info("Bridge ready on port %d", lifecycle.port)

        # Import URDF if requested
        if args.urdf is not None:
            urdf_path = str(Path(args.urdf).resolve())
            logger.info("Importing URDF: %s", urdf_path)
            result = lifecycle.import_urdf(urdf_path)
            logger.info(
                "Import result: prim_path=%s, joints=%d, links=%d",
                result.get("prim_path", "?"),
                result.get("joint_count", 0),
                result.get("link_count", 0),
            )

        # Screenshot if requested
        if args.screenshot:
            screenshot_path = args.screenshot_path or "isaac_lifecycle_screenshot.png"
            logger.info("Taking screenshot -> %s", screenshot_path)
            result = lifecycle.screenshot(path=screenshot_path)
            logger.info("Screenshot saved: %s", result.get("saved_to", screenshot_path))

        if args.exit_after:
            logger.info("--exit-after set, shutting down")
            return 0

        # Stay alive for interactive inspection
        logger.info("Lifecycle running. Press Ctrl+C to stop.")
        while not shutdown_requested:
            time.sleep(0.5)

        return 0

    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        logger.error("Fatal error: %s", exc, exc_info=True)
        return 1
    finally:
        lifecycle.stop()


if __name__ == "__main__":
    sys.exit(main())
