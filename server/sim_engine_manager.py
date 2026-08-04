"""Unified simulation engine lifecycle management.

Provides start/stop/status for every engine the registry knows — which ones
those are is descriptor data (``engines.d/``), not a list in this module.

An engine with a launch command runs as a subprocess managed here; one
without (a user-run daemon or a remote engine) is attached to.  If an engine
is already running (responds to health check), ``start_engine`` returns early.

Thread-safe: all engine state is guarded by ``_lock``.
"""

from __future__ import annotations

import json
import logging
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from server.engine_registry import (
    engine_names,
    launch_argv,
    resolve_cwd,
    resolve_host,
    resolve_port,
)
from server.engine_registry import (
    install_hint as descriptor_hint,
)

logger = logging.getLogger("solidmind.sim_engine_manager")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Engine status enum + state dataclass
# ---------------------------------------------------------------------------


class EngineStatus(str, Enum):
    STOPPED = "stopped"
    INIT = "init"
    READY = "ready"
    RUNNING = "running"
    DRAINING = "draining"
    FAILED = "failed"


@dataclass(slots=True)
class EngineState:
    backend: str
    status: EngineStatus = EngineStatus.STOPPED
    port: int = 0
    pid: int | None = None
    process: subprocess.Popen[bytes] | None = None
    started_at: float = 0.0
    last_health: float = 0.0
    error: str = ""


# ---------------------------------------------------------------------------
# Configuration (env vars + defaults)
# ---------------------------------------------------------------------------


def _get_host() -> str:
    return resolve_host("")


def _get_port(backend: str) -> int:
    return resolve_port(backend)


# ---------------------------------------------------------------------------
# Thread-safe state registry
# ---------------------------------------------------------------------------

_lock = threading.RLock()
_engines: dict[str, EngineState] = {}
_shutdown_event = threading.Event()
_monitor_thread: threading.Thread | None = None


def _valid_backends() -> frozenset[str]:
    """The engines the registry knows — data, not a table in core."""
    return frozenset(engine_names())


def _get_or_create_state(backend: str) -> EngineState:
    """Get existing state or create a new STOPPED state.  Caller must hold _lock."""
    if backend not in _engines:
        _engines[backend] = EngineState(
            backend=backend,
            port=_get_port(backend),
        )
    return _engines[backend]


# ---------------------------------------------------------------------------
# Health check (protocol-level JSON ping)
# ---------------------------------------------------------------------------


def _health_check(host: str, port: int, timeout: float = 2.0) -> tuple[bool, dict[str, Any]]:
    """Send ``{"cmd": "ping", "args": {}}`` and expect ``{"ok": true}``.

    Returns (healthy, response_dict).  On failure returns (False, {"error": ...}).
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect((host, port))
        msg = json.dumps({"cmd": "ping", "args": {}}) + "\n"
        sock.sendall(msg.encode())
        data = b""
        while b"\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        if data:
            resp = json.loads(data.decode().strip())
            return resp.get("ok", False), resp
        return False, {"error": "empty response"}
    except (ConnectionRefusedError, OSError, TimeoutError, json.JSONDecodeError) as exc:
        return False, {"error": str(exc)}
    finally:
        sock.close()


def _tcp_ping(host: str, port: int, timeout: float = 2.0) -> bool:
    """Fallback: check if a TCP server is listening (no protocol)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect((host, port))
        return True
    except (ConnectionRefusedError, OSError, TimeoutError):
        return False
    finally:
        sock.close()


def _port_available(host: str, port: int) -> bool:
    """Check if a port is available for binding."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.close()
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Engine start
# ---------------------------------------------------------------------------


def start_engine(
    backend: str,
    *,
    port: int | None = None,
    headless: bool = True,
    timeout_s: float = 30.0,
    runtime: str = "stub",
) -> dict[str, Any]:
    """Start a simulation backend subprocess.

    Parameters
    ----------
    backend : str
        Any registered engine name (see ``sim.engine_status``).
    port : int | None
        Override default port.
    headless : bool
        Run in headless mode (default True).
    timeout_s : float
        Seconds to wait for the backend to accept connections.
    runtime : str
        For Gazebo: 'stub' (no Gazebo needed) or 'real'.

    Returns
    -------
    dict
        ``{"ok": True, "status": "started"|"already_running", ...}``
    """
    backend = backend.strip().lower()
    if backend not in _valid_backends():
        return _error(
            "UNKNOWN_BACKEND",
            f"Unknown backend {backend!r}. Available: {sorted(_valid_backends())}",
        )

    host = _get_host()

    with _lock:
        state = _get_or_create_state(backend)
        actual_port = port or state.port

        # Already running and healthy?
        if state.status in (EngineStatus.READY, EngineStatus.RUNNING):
            if state.process and state.process.poll() is None:
                healthy, _ = _health_check(host, actual_port, timeout=2.0)
                if healthy:
                    state.last_health = time.monotonic()
                    logger.info(
                        "engine_already_running",
                        extra={"backend": backend, "port": actual_port, "pid": state.pid},
                    )
                    return {
                        "ok": True,
                        "status": "already_running",
                        "backend": backend,
                        "host": host,
                        "port": actual_port,
                        "pid": state.pid,
                        "engine_status": state.status.value,
                    }
            # Process died but state wasn't updated
            if state.process and state.process.poll() is not None:
                state.status = EngineStatus.FAILED
                state.error = f"Process exited (rc={state.process.returncode})"

        # Also check if something external is listening
        if _tcp_ping(host, actual_port, timeout=1.0):
            logger.info(
                "engine_external_running",
                extra={"backend": backend, "port": actual_port},
            )
            state.status = EngineStatus.READY
            state.port = actual_port
            state.last_health = time.monotonic()
            return {
                "ok": True,
                "status": "already_running",
                "backend": backend,
                "host": host,
                "port": actual_port,
                "pid": None,
                "engine_status": state.status.value,
            }

        # Validate port availability
        if not _port_available(host, actual_port):
            return _error(
                "PORT_UNAVAILABLE",
                f"Port {actual_port} is already in use but not responding to health check",
            )

        # Transition to INIT
        state.status = EngineStatus.INIT
        state.port = actual_port
        state.error = ""

    # Launch from the descriptor (outside the lock — spawning can be slow).
    result = _launch_from_descriptor(
        backend,
        port=actual_port,
        headless=headless,
        runtime=runtime,
        timeout_s=timeout_s,
    )

    # Update state based on result
    with _lock:
        state = _get_or_create_state(backend)
        if result.get("ok"):
            state.status = EngineStatus.READY
            state.pid = result.get("pid")
            state.started_at = time.monotonic()
            state.last_health = time.monotonic()
            state.error = ""
            result["engine_status"] = EngineStatus.READY.value
            logger.info(
                "engine_started",
                extra={
                    "backend": backend,
                    "port": actual_port,
                    "pid": state.pid,
                    "status": "ready",
                },
            )
        else:
            state.status = EngineStatus.FAILED
            state.error = result.get("error", {}).get("message", "unknown error")

    return result


def _launch_from_descriptor(
    backend: str,
    *,
    port: int,
    headless: bool,
    runtime: str,
    timeout_s: float,
) -> dict[str, Any]:
    """Start *backend* using its descriptor's launch command.

    Nothing here knows which engine it is starting: the argv, the working
    directory and the install hint all come from ``engines.d`` (see
    ``docs/engine-contract.md`` §9).  A descriptor without a launch command is
    attach-only — a user-managed daemon or a remote engine — and says so
    rather than pretending it can spawn one.
    """
    # Variants let one descriptor carry the handful of launch shapes an engine
    # has (Gazebo's real runtime, Isaac with a GUI) without core branching.
    variant: str | None = None
    if runtime and runtime != "stub":
        variant = runtime
    elif not headless:
        variant = "gui"

    argv = launch_argv(backend, port=port, variant=variant, extra={"RUNTIME": runtime})
    if argv is None:
        hint = descriptor_hint(backend)
        return _error(
            "ENGINE_ATTACH_ONLY",
            f"The {backend!r} descriptor has no launch command — start it yourself, "
            f"then core will attach on port {port}." + (f"\n{hint}" if hint else ""),
        )

    return _launch_subprocess(backend, argv, port, timeout_s, cwd=resolve_cwd(backend))


def _launch_subprocess(
    backend: str,
    cmd: list[str],
    port: int,
    timeout_s: float,
    cwd: str | None = None,
) -> dict[str, Any]:
    """Generic subprocess launcher with TCP readiness wait."""
    host = _get_host()
    logger.info("Launching %s: %s", backend, " ".join(cmd))

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
        )
    except FileNotFoundError as exc:
        return _error(
            f"{backend.upper()}_LAUNCH_FAILED",
            f"Failed to launch {backend}: {exc}",
        )
    except PermissionError as exc:
        return _error(
            f"{backend.upper()}_LAUNCH_FAILED",
            f"Permission denied launching {backend}: {exc}",
        )

    with _lock:
        state = _get_or_create_state(backend)
        state.process = proc
        state.pid = proc.pid

    # Wait for TCP readiness
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            stderr = ""
            try:
                _, err = proc.communicate(timeout=1)
                stderr = err.decode(errors="replace")[:500]
            except Exception:
                pass
            return _error(
                f"{backend.upper()}_CRASHED",
                f"{backend} exited with rc={proc.returncode}: {stderr}",
            )

        if _tcp_ping(host, port, timeout=1.0):
            logger.info("%s ready on %s:%d (pid=%d)", backend, host, port, proc.pid)
            return {
                "ok": True,
                "status": "started",
                "backend": backend,
                "host": host,
                "port": port,
                "pid": proc.pid,
            }

        time.sleep(0.5)

    # Timeout — kill and report
    proc.terminate()
    return _error(
        f"{backend.upper()}_TIMEOUT",
        f"{backend} did not become ready within {timeout_s}s",
    )


# ---------------------------------------------------------------------------
# Engine stop (graceful with draining)
# ---------------------------------------------------------------------------


def stop_engine(backend: str, *, drain_timeout_s: float = 5.0) -> dict[str, Any]:
    """Stop a simulation backend subprocess with graceful draining.

    1. Set state → DRAINING
    2. Send shutdown command if bridge supports it
    3. Wait drain_timeout_s for process to exit
    4. SIGTERM
    5. Wait 5s
    6. SIGKILL if needed
    7. Set state → STOPPED
    """
    backend = backend.strip().lower()
    if backend not in _valid_backends():
        return _error("UNKNOWN_BACKEND", f"Unknown backend {backend!r}")

    with _lock:
        state = _get_or_create_state(backend)

        if state.status == EngineStatus.STOPPED:
            return {"ok": True, "status": "not_running", "backend": backend}

        proc = state.process
        if proc is None or proc.poll() is not None:
            state.status = EngineStatus.STOPPED
            state.process = None
            state.pid = None
            state.error = ""
            return {"ok": True, "status": "not_running", "backend": backend}

        pid = proc.pid
        state.status = EngineStatus.DRAINING
        logger.info(
            "engine_draining",
            extra={"backend": backend, "pid": pid},
        )

    # Try sending shutdown command via protocol
    host = _get_host()
    _send_shutdown(host, state.port)

    # Wait for graceful exit
    try:
        proc.wait(timeout=drain_timeout_s)
    except subprocess.TimeoutExpired:
        # SIGTERM
        logger.info("engine_sigterm", extra={"backend": backend, "pid": pid})
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            # SIGKILL
            logger.warning("engine_sigkill", extra={"backend": backend, "pid": pid})
            proc.kill()
            proc.wait(timeout=2)

    with _lock:
        state = _get_or_create_state(backend)
        state.status = EngineStatus.STOPPED
        state.process = None
        state.pid = None
        state.error = ""
        logger.info(
            "engine_stopped",
            extra={"backend": backend, "pid": pid},
        )

    return {"ok": True, "status": "stopped", "backend": backend, "pid": pid}


def _send_shutdown(host: str, port: int) -> None:
    """Best-effort: send a shutdown command to the bridge."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2.0)
        sock.connect((host, port))
        msg = json.dumps({"command": "shutdown"}) + "\n"
        sock.sendall(msg.encode())
        sock.close()
    except (ConnectionRefusedError, OSError, TimeoutError):
        pass


# ---------------------------------------------------------------------------
# Engine status
# ---------------------------------------------------------------------------


def engine_status() -> dict[str, Any]:
    """Report status of all simulation backends."""
    host = _get_host()
    statuses: dict[str, dict[str, Any]] = {}

    with _lock:
        for backend in _valid_backends():
            state = _get_or_create_state(backend)
            port = state.port

            # Check for crashed processes
            if state.status in (EngineStatus.READY, EngineStatus.RUNNING):
                if state.process and state.process.poll() is not None:
                    state.status = EngineStatus.FAILED
                    state.error = f"Process exited (rc={state.process.returncode})"

            # Try health check for ready/running engines
            healthy = False
            if state.status in (EngineStatus.READY, EngineStatus.RUNNING):
                healthy, _ = _health_check(host, port, timeout=1.0)
                if healthy:
                    state.last_health = time.monotonic()

            hint = ""
            if state.status in (EngineStatus.STOPPED, EngineStatus.FAILED):
                hint = descriptor_hint(backend)

            statuses[backend] = {
                "status": state.status.value,
                "port": port,
                "pid": state.pid,
                "managed": state.process is not None and state.process.poll() is None,
                "healthy": healthy,
                "error": state.error or None,
                "uptime_s": round(time.monotonic() - state.started_at, 1)
                if state.started_at and state.status in (EngineStatus.READY, EngineStatus.RUNNING)
                else None,
                "install_hint": hint or None,
            }

    return {"ok": True, "engines": statuses}


# ---------------------------------------------------------------------------
# Monitor thread (crash detection, opt-in)
# ---------------------------------------------------------------------------


def start_monitor(interval_s: float = 10.0) -> None:
    """Start background monitor thread for crash detection.

    Does NOT auto-restart engines — just updates status to FAILED and logs.
    """
    global _monitor_thread
    with _lock:
        if _monitor_thread is not None and _monitor_thread.is_alive():
            return
        _shutdown_event.clear()
        _monitor_thread = threading.Thread(
            target=_monitor_loop,
            args=(interval_s,),
            daemon=True,
            name="sim-engine-monitor",
        )
        _monitor_thread.start()
        logger.info("engine_monitor_started", extra={"interval_s": interval_s})


def stop_monitor() -> None:
    """Stop the background monitor thread."""
    global _monitor_thread
    _shutdown_event.set()
    if _monitor_thread is not None:
        _monitor_thread.join(timeout=15.0)
        _monitor_thread = None
        logger.info("engine_monitor_stopped")


def _monitor_loop(interval_s: float) -> None:
    """Periodic health check, detect crashes."""
    host = _get_host()
    while not _shutdown_event.is_set():
        with _lock:
            for backend in _valid_backends():
                state = _engines.get(backend)
                if state is None:
                    continue
                if state.status in (EngineStatus.READY, EngineStatus.RUNNING):
                    if state.process and state.process.poll() is not None:
                        state.status = EngineStatus.FAILED
                        state.error = f"Process exited (rc={state.process.returncode})"
                        logger.error(
                            "engine_crashed",
                            extra={"backend": backend, "error": state.error},
                        )
                    else:
                        # Protocol-level health check
                        healthy, _ = _health_check(host, state.port, timeout=2.0)
                        if healthy:
                            state.last_health = time.monotonic()
        _shutdown_event.wait(interval_s)


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


def shutdown_all() -> None:
    """Stop all managed engines and the monitor thread.  Call at server exit."""
    stop_monitor()
    with _lock:
        backends = list(_engines.keys())
    for backend in backends:
        stop_engine(backend)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}
