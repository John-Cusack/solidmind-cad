"""FreeCAD startup script — runs inside FreeCAD's embedded Python interpreter.

Launched via: AppRun FreeCADCmd /app/docker/freecad_startup.py

Starts the SolidMind addon socket server in headless mode.

Key insight: FreeCAD commands must run on the main thread (Qt thread
safety). In GUI mode, QTimer dispatches from the job queue. In Docker
headless mode, we skip QTimer but manually poll the job queue from
this main-thread loop.
"""
import os
import queue
import sys
import traceback

# Add the solidmind-cad repo root to FreeCAD's Python path
sys.path.insert(0, "/app")

import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
)
logger = logging.getLogger("solidmind.docker")

host = os.environ.get("FREECAD_HOST", "0.0.0.0")
port = int(os.environ.get("FREECAD_PORT", "9876"))

# Do NOT set SOLIDMIND_HEADLESS — we want jobs to go through the queue
# so we can process them on the main thread below.
os.environ.pop("SOLIDMIND_HEADLESS", None)

# Start the socket server. QTimer will be created but won't fire
# because FreeCADCmd has no event loop. That's OK — we poll the
# job queue manually below.
import freecad_addon.socket_server as _ss_mod  # noqa: E402  interleaved with boot steps

_ss_mod.start_server(host=host, port=port)
logger.info("Socket server started on %s:%d", host, port)

# Get a reference to the server's job queue and response type
from freecad_addon.protocol import Response  # noqa: E402,I001  interleaved with boot steps

server = _ss_mod._server
if server is None:
    logger.error("Server not started!")
    sys.exit(1)

logger.info("Main thread polling loop started (processing jobs on main thread)")

# Main-thread polling loop: process jobs from the socket server's queue.
# This replaces QTimer in headless Docker mode.
try:
    while True:
        try:
            job = server._job_queue.get(timeout=0.01)  # 10ms polling
        except queue.Empty:
            continue

        try:
            result = job.handler(**job.args)
            job.response = Response.success(result)
        except Exception as e:
            tb = traceback.format_exc()
            logger.error("Command failed: %s\n%s", e, tb)
            job.response = Response.failure(f"{type(e).__name__}: {e}")
        finally:
            job.event.set()

except KeyboardInterrupt:
    from freecad_addon.socket_server import stop_server
    stop_server()
