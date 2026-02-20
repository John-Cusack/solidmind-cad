"""TCP bridge server for Isaac simulation/teleop commands."""
from __future__ import annotations

import argparse
import logging
import signal
import socket
import threading
from typing import Any

from isaac_bridge.protocol import (
    ProtocolError,
    encode_response,
    error_response,
    ok_response,
    parse_request_line,
)
from isaac_bridge.runtime_isaac import IsaacRuntime, IsaacRuntimeError, main_thread_dispatcher

logger = logging.getLogger("solidmind.isaac_bridge")


class BridgeServer:
    """Newline-delimited JSON TCP server for Isaac command handling."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 9878,
        headless: bool = True,
    ) -> None:
        self._host = host
        self._port = port
        self._headless = headless
        self._runtime = IsaacRuntime(headless=headless)
        self._sock: socket.socket | None = None
        self._stop_event = threading.Event()

    @property
    def port(self) -> int:
        return self._port

    def serve_forever(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.settimeout(0.2)
        srv.bind((self._host, self._port))
        srv.listen(8)
        self._sock = srv
        self._port = int(srv.getsockname()[1])

        logger.info(
            "Isaac bridge listening on %s:%d (headless=%s)",
            self._host,
            self._port,
            self._headless,
        )

        try:
            while not self._stop_event.is_set():
                try:
                    conn, addr = srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                thread = threading.Thread(
                    target=self._handle_connection,
                    args=(conn, addr),
                    daemon=True,
                )
                thread.start()
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        self._stop_event.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _handle_connection(self, conn: socket.socket, addr: Any) -> None:
        peer = f"{addr[0]}:{addr[1]}" if isinstance(addr, tuple) and len(addr) >= 2 else "unknown"
        logger.info("Client connected: %s", peer)
        with conn:
            conn.settimeout(1.0)
            buf = b""
            while not self._stop_event.is_set():
                try:
                    data = conn.recv(65536)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    response = self._handle_line(line)
                    try:
                        conn.sendall(encode_response(response))
                    except OSError:
                        return
        logger.info("Client disconnected: %s", peer)

    def _handle_line(self, line: bytes) -> dict[str, Any]:
        try:
            cmd, args = parse_request_line(line)
        except ProtocolError as exc:
            return error_response(exc.code, exc.message)

        import time as _time
        logger.info("=> %s (thread=%s)", cmd, threading.current_thread().name)
        t0 = _time.monotonic()
        try:
            if cmd == "ping":
                result = self._runtime.ping()
            elif cmd == "import_urdf":
                result = self._runtime.import_urdf(
                    urdf_path=_require_str(args, "urdf_path"),
                    import_config=_optional_object(args, "import_config"),
                )
            elif cmd == "diagnose":
                result = self._runtime.diagnose(
                    prim_path=_optional_str(args, "prim_path") or "/",
                )
            elif cmd == "reload":
                reload_result = self._runtime.reload()
                new_runtime = reload_result.pop("new_runtime", None)
                if new_runtime is not None:
                    self._runtime = new_runtime
                result = reload_result
            elif cmd == "simulate":
                result = self._runtime.simulate(
                    mechanism=_optional_object(args, "mechanism"),
                    duration_s=_optional_float(args, "duration_s", 1.0),
                    dt_s=_optional_float(args, "dt_s", 0.001),
                    output_interval=_optional_float(args, "output_interval", 0.01),
                    profile=_optional_object(args, "profile"),
                    urdf_path=_optional_str(args, "urdf_path"),
                    import_config=_optional_object(args, "import_config"),
                )
            elif cmd == "simulate_start":
                result = self._runtime.simulate_start(
                    mechanism=_optional_object(args, "mechanism"),
                    duration_s=_optional_float(args, "duration_s", 1.0),
                    dt_s=_optional_float(args, "dt_s", 0.001),
                    output_interval=_optional_float(args, "output_interval", 0.01),
                    profile=_optional_object(args, "profile"),
                    urdf_path=_optional_str(args, "urdf_path"),
                    import_config=_optional_object(args, "import_config"),
                )
            elif cmd == "simulate_status":
                result = self._runtime.simulate_status(
                    session_id=_require_str(args, "session_id"),
                )
            elif cmd == "simulate_stop":
                result = self._runtime.simulate_stop(
                    session_id=_require_str(args, "session_id"),
                )
            elif cmd == "teleop_start":
                result = self._runtime.teleop_start(
                    mechanism=_require_object(args, "mechanism"),
                    profile=_optional_object(args, "profile"),
                    urdf_path=_optional_str(args, "urdf_path"),
                    import_config=_optional_object(args, "import_config"),
                )
            elif cmd == "teleop_command":
                result = self._runtime.teleop_command(
                    session_id=_require_str(args, "session_id"),
                    vx_mps=_optional_float(args, "vx_mps", 0.0),
                    yaw_rate_rps=_optional_float(args, "yaw_rate_rps", 0.0),
                    body_height_m=_optional_float(args, "body_height_m", 0.0),
                )
            elif cmd == "teleop_state":
                result = self._runtime.teleop_state(
                    session_id=_require_str(args, "session_id"),
                )
            elif cmd == "teleop_stop":
                result = self._runtime.teleop_stop(
                    session_id=_require_str(args, "session_id"),
                )
            elif cmd == "screenshot":
                result = self._runtime.screenshot(
                    width=int(_optional_float(args, "width", 1280)),
                    height=int(_optional_float(args, "height", 720)),
                    camera_position=args.get("camera_position"),
                    camera_target=args.get("camera_target"),
                )
            else:
                logger.info("<= %s UNKNOWN_COMMAND (%.3fs)", cmd, _time.monotonic() - t0)
                return error_response("UNKNOWN_COMMAND", f"Unknown command: {cmd}")
            logger.info("<= %s OK (%.3fs)", cmd, _time.monotonic() - t0)
            return ok_response(result)
        except IsaacRuntimeError as exc:
            logger.info("<= %s ERROR %s (%.3fs)", cmd, exc.code, _time.monotonic() - t0)
            return error_response(exc.code, exc.message, details=exc.details)
        except ValueError as exc:
            logger.info("<= %s ERROR INVALID_ARGS (%.3fs)", cmd, _time.monotonic() - t0)
            return error_response("INVALID_ARGS", str(exc))
        except Exception as exc:
            # After a hot-reload, IsaacRuntimeError from the reloaded module
            # won't match the old class reference.  Duck-type check.
            if hasattr(exc, "code") and hasattr(exc, "message") and hasattr(exc, "details"):
                logger.info("<= %s ERROR %s (%.3fs, post-reload)", cmd, exc.code, _time.monotonic() - t0)
                return error_response(exc.code, exc.message, details=exc.details)
            logger.exception("Unhandled error while processing '%s'", cmd)
            logger.info("<= %s ERROR INTERNAL (%.3fs)", cmd, _time.monotonic() - t0)
            return error_response("INTERNAL_ERROR", str(exc))


def _require_str(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"'{key}' must be a non-empty string")
    return value.strip()


def _require_object(args: dict[str, Any], key: str) -> dict[str, Any]:
    value = args.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"'{key}' must be an object")
    return value


def _optional_object(args: dict[str, Any], key: str) -> dict[str, Any] | None:
    if key not in args or args.get(key) is None:
        return None
    value = args.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"'{key}' must be an object when provided")
    return value


def _optional_float(args: dict[str, Any], key: str, default: float) -> float:
    if key not in args:
        return default
    value = args.get(key)
    if not isinstance(value, (int, float)):
        raise ValueError(f"'{key}' must be numeric")
    return float(value)


def _optional_str(args: dict[str, Any], key: str) -> str | None:
    if key not in args or args.get(key) is None:
        return None
    value = args.get(key)
    if not isinstance(value, str):
        raise ValueError(f"'{key}' must be a string when provided")
    return value.strip() or None


def _pump_main_thread(server: BridgeServer) -> None:
    """Pump the Kit event loop, main-thread dispatcher, and teleop tick.

    Isaac Sim requires its Kit event loop to be pumped on the main thread
    for World creation, URDF import, and physics stepping to complete —
    even in headless mode.  The TCP bridge always runs in a background
    thread; this function occupies the main thread.

    Teleop tick runs after dispatcher processing so that any
    ``teleop_command`` mutations are visible before the next controller
    computation.  ``dt_s`` is computed from ``time.monotonic()`` deltas
    and bounded to [0.0001, 0.1] to guard against clock jitter and
    long stalls.  Gait timing is approximate — coupled to Kit's frame
    rate (typically ~60 Hz) when Kit is available.
    """
    import time as _t

    app = None
    try:
        import omni.kit.app  # type: ignore[import-not-found]
        app = omni.kit.app.get_app()
    except Exception:
        logger.info("omni.kit.app not available — dispatcher-only pump mode")

    label = "Kit + dispatcher" if app else "dispatcher-only"
    logger.info("Main-thread pump started (%s)", label)

    _DT_MIN = 0.0001  # 0.1 ms — guard against zero/negative dt
    _DT_MAX = 0.1     # 100 ms — guard against long stalls
    last_t = _t.monotonic()

    while not server._stop_event.is_set():
        if app is not None:
            try:
                app.update()
            except Exception:
                break
        main_thread_dispatcher.process_pending()

        # Compute bounded dt_s from wall clock.
        now = _t.monotonic()
        raw_dt = now - last_t
        last_t = now
        dt_s = max(_DT_MIN, min(_DT_MAX, raw_dt))

        # Tick active teleop sessions (controller + physics step).
        try:
            server._runtime.tick_teleop(dt_s)
        except Exception as exc:
            logger.warning("tick_teleop error (non-fatal): %s", exc)

        if app is None:
            # No Kit — avoid busy-spin
            _t.sleep(0.005)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="SolidMind Isaac bridge TCP server")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=9878, help="Bind port (default: 9878)")
    parser.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Run runtime in headless mode (default: true)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging level (default: INFO)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
    )

    server = BridgeServer(host=args.host, port=args.port, headless=args.headless)

    def _signal_handler(_signum: int, _frame: Any) -> None:
        server.shutdown()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # Always run the bridge in a background thread and pump Kit / dispatcher
    # on the main thread.  Isaac Sim requires Kit event loop updates on the
    # main thread for World creation, URDF import, and physics stepping —
    # even in headless mode.
    main_thread_dispatcher.enable()
    logger.info("Main-thread dispatcher enabled (headless=%s)", args.headless)
    bridge_thread = threading.Thread(
        target=server.serve_forever,
        daemon=True,
        name="isaac-bridge-server",
    )
    bridge_thread.start()
    try:
        _pump_main_thread(server)
    finally:
        server.shutdown()
        bridge_thread.join(timeout=5.0)


if __name__ == "__main__":
    main()
