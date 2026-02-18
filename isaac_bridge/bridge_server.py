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
from isaac_bridge.runtime_isaac import IsaacRuntime, IsaacRuntimeError

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

        try:
            if cmd == "ping":
                return ok_response(self._runtime.ping())
            if cmd == "simulate":
                mechanism = _require_object(args, "mechanism")
                profile = _optional_object(args, "profile")
                result = self._runtime.simulate(
                    mechanism=mechanism,
                    duration_s=_optional_float(args, "duration_s", 1.0),
                    dt_s=_optional_float(args, "dt_s", 0.001),
                    output_interval=_optional_float(args, "output_interval", 0.01),
                    profile=profile,
                )
                return ok_response(result)
            if cmd == "teleop_start":
                mechanism = _require_object(args, "mechanism")
                profile = _optional_object(args, "profile")
                result = self._runtime.teleop_start(mechanism=mechanism, profile=profile)
                return ok_response(result)
            if cmd == "teleop_command":
                result = self._runtime.teleop_command(
                    session_id=_require_str(args, "session_id"),
                    vx_mps=_optional_float(args, "vx_mps", 0.0),
                    yaw_rate_rps=_optional_float(args, "yaw_rate_rps", 0.0),
                    body_height_m=_optional_float(args, "body_height_m", 0.0),
                )
                return ok_response(result)
            if cmd == "teleop_state":
                result = self._runtime.teleop_state(
                    session_id=_require_str(args, "session_id"),
                )
                return ok_response(result)
            if cmd == "teleop_stop":
                result = self._runtime.teleop_stop(
                    session_id=_require_str(args, "session_id"),
                )
                return ok_response(result)
            return error_response("UNKNOWN_COMMAND", f"Unknown command: {cmd}")
        except IsaacRuntimeError as exc:
            return error_response(exc.code, exc.message, details=exc.details)
        except ValueError as exc:
            return error_response("INVALID_ARGS", str(exc))
        except Exception as exc:
            logger.exception("Unhandled error while processing command")
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
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
    )

    server = BridgeServer(host=args.host, port=args.port, headless=args.headless)

    def _signal_handler(_signum: int, _frame: Any) -> None:
        server.shutdown()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    server.serve_forever()


if __name__ == "__main__":
    main()
