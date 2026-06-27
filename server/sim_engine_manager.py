"""Unified simulation engine lifecycle management.

Provides start/stop/status for all simulation backends:
- **chrono**: C++ multibody dynamics daemon (gear trains, linkages)
- **gazebo**: CPU physics + ROS/PX4 (drones, wheeled vehicles)
- **isaac**: GPU physics (legged robots, articulated mechanisms)

Each backend can run as a subprocess managed by this module.  If a backend
is already running (responds to health check), ``start_engine`` returns early.

Thread-safe: all engine state is guarded by ``_lock``.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

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

_DEFAULT_PORTS: dict[str, int] = {
    "chrono": 9877,
    "gazebo": 9879,
    "isaac": 9878,
}

_DEFAULT_HOST = "127.0.0.1"


def _get_host() -> str:
    return os.environ.get("SOLIDMIND_SIM_HOST", _DEFAULT_HOST)


def _get_port(backend: str) -> int:
    env_key = f"SOLIDMIND_{backend.upper()}_PORT"
    env_val = os.environ.get(env_key)
    if env_val is not None:
        try:
            return int(env_val)
        except ValueError:
            logger.warning("Invalid %s=%r, using default", env_key, env_val)
    return _DEFAULT_PORTS[backend]


# ---------------------------------------------------------------------------
# Thread-safe state registry
# ---------------------------------------------------------------------------

_lock = threading.RLock()
_engines: dict[str, EngineState] = {}
_shutdown_event = threading.Event()
_monitor_thread: threading.Thread | None = None

VALID_BACKENDS = frozenset(_DEFAULT_PORTS.keys())


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
        One of: 'chrono', 'gazebo', 'isaac'.
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
    if backend not in VALID_BACKENDS:
        return _error(
            "UNKNOWN_BACKEND",
            f"Unknown backend {backend!r}. Available: {sorted(VALID_BACKENDS)}",
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

    # Delegate to backend-specific launcher (outside lock for potentially slow ops)
    if backend == "isaac":
        result = _start_isaac(actual_port, headless, timeout_s)
    elif backend == "gazebo":
        result = _start_gazebo(actual_port, runtime, timeout_s)
    elif backend == "chrono":
        result = _start_chrono(actual_port, timeout_s)
    else:
        result = _error("UNKNOWN_BACKEND", f"No launcher for {backend!r}")

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


def _start_chrono(port: int, timeout_s: float) -> dict[str, Any]:
    """Start the Chrono daemon subprocess."""
    run_script = _PROJECT_ROOT / "chrono_daemon" / "run.sh"
    build_binary = _PROJECT_ROOT / "chrono_daemon" / "build" / "chrono_daemon"

    if build_binary.is_file():
        cmd = [str(build_binary), "--port", str(port)]
    elif run_script.is_file():
        cmd = ["bash", str(run_script), "--port", str(port)]
    else:
        return _error(
            "CHRONO_NOT_BUILT",
            "Chrono daemon not found. Build it with:\n"
            "  cd chrono_daemon && mkdir -p build && cd build\n"
            "  cmake .. && make -j$(nproc)\n"
            "Or install as a systemd service:\n"
            "  chrono_daemon/install-service.sh",
        )

    return _launch_subprocess("chrono", cmd, port, timeout_s)


def _start_gazebo(port: int, runtime: str, timeout_s: float) -> dict[str, Any]:
    """Start the Gazebo bridge subprocess."""
    script = _PROJECT_ROOT / "scripts" / "run_gazebo_bridge.sh"

    if runtime == "stub":
        cmd = [
            "python3",
            "-m",
            "gazebo_bridge.bridge_server",
            "--port",
            str(port),
            "--runtime",
            "stub",
        ]
    elif script.is_file():
        cmd = [
            "bash",
            str(script),
            "--port",
            str(port),
            "--runtime",
            runtime,
        ]
    else:
        return _error(
            "GAZEBO_LAUNCH_FAILED",
            "Gazebo bridge launcher not found. Run in stub mode:\n"
            "  sim.start_engine('gazebo', runtime='stub')\n"
            "Or install Gazebo Harmonic and use:\n"
            "  scripts/run_gazebo_bridge.sh --runtime real",
        )

    return _launch_subprocess("gazebo", cmd, port, timeout_s)


def _start_isaac(port: int, headless: bool, timeout_s: float) -> dict[str, Any]:
    """Start the Isaac bridge subprocess."""
    try:
        from server.isaac_adapter import launch_bridge

        return launch_bridge(headless=headless, port=port, timeout_s=timeout_s)
    except ImportError:
        pass

    script = _PROJECT_ROOT / "scripts" / "run_isaac_bridge.sh"
    if not script.is_file():
        return _error(
            "ISAAC_LAUNCH_FAILED",
            "Isaac bridge launcher not found. Set ISAAC_PYTHON env var or\n"
            "ensure scripts/run_isaac_bridge.sh exists.",
        )

    cmd = ["bash", str(script), "--port", str(port)]
    if headless:
        cmd.append("--headless")

    return _launch_subprocess("isaac", cmd, port, timeout_s)


def _launch_subprocess(
    backend: str,
    cmd: list[str],
    port: int,
    timeout_s: float,
) -> dict[str, Any]:
    """Generic subprocess launcher with TCP readiness wait."""
    host = _get_host()
    logger.info("Launching %s: %s", backend, " ".join(cmd))

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
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
    if backend not in VALID_BACKENDS:
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
        for backend in VALID_BACKENDS:
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

            install_hint = ""
            if state.status in (EngineStatus.STOPPED, EngineStatus.FAILED):
                if backend == "chrono":
                    binary = _PROJECT_ROOT / "chrono_daemon" / "build" / "chrono_daemon"
                    if binary.is_file():
                        install_hint = (
                            "Binary found but not running. Start with: sim.start_engine('chrono')"
                        )
                    else:
                        install_hint = "Not built. cd chrono_daemon && mkdir build && cd build && cmake .. && make"
                elif backend == "gazebo":
                    install_hint = (
                        "Start stub: sim.start_engine('gazebo'). Real: install Gazebo Harmonic"
                    )
                elif backend == "isaac":
                    install_hint = "Set ISAAC_PYTHON env var, then: sim.start_engine('isaac')"

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
                "install_hint": install_hint or None,
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
            for backend in VALID_BACKENDS:
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
