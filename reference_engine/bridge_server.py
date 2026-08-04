"""TCP server for the reference engine.

The ~60 lines of NDJSON framing here are duplicated from the other bridges on
purpose (architecture doc, Principle 2): an engine author copying this file
gets a complete, working engine with no shared library to install.

Run it with::

    python3 -m reference_engine.bridge_server --port 9880
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import socket
import threading
from typing import Any

from reference_engine.runtime import ReferenceError, ReferenceRuntime

logger = logging.getLogger("solidmind.reference_engine")


class ReferenceBridgeServer:
    """Newline-delimited JSON TCP server speaking contract v1."""

    def __init__(self, *, host: str = "127.0.0.1", port: int = 9880) -> None:
        self._host = host
        self._port = port
        self._runtime = ReferenceRuntime()
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
        logger.info("Reference engine listening on %s:%d", self._host, self._port)

        try:
            while not self._stop_event.is_set():
                try:
                    conn, addr = srv.accept()
                except TimeoutError:
                    continue
                except OSError:
                    break
                threading.Thread(
                    target=self._handle_connection, args=(conn, addr), daemon=True
                ).start()
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
        buf = b""
        try:
            conn.settimeout(0.5)
            while not self._stop_event.is_set():
                try:
                    data = conn.recv(65536)
                except TimeoutError:
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
                    conn.sendall(self._dispatch(line).encode("utf-8"))
        except Exception:
            logger.exception("Error handling connection from %s", addr)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    # -- protocol --------------------------------------------------------

    def _encode(self, payload: dict[str, Any], request_id: str | None) -> str:
        """One response line; ``request_id`` echoed verbatim when present."""
        if request_id is not None:
            payload = {**payload, "request_id": request_id}
        return json.dumps(payload) + "\n"

    def _dispatch(self, line: bytes) -> str:
        try:
            msg = json.loads(line.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            return self._encode(
                {
                    "ok": False,
                    "error": {"code": "INVALID_JSON", "message": f"Malformed JSON: {exc}"},
                },
                None,
            )

        request_id = msg.get("request_id") if isinstance(msg, dict) else None
        if not isinstance(request_id, str):
            request_id = None

        if not isinstance(msg, dict) or not isinstance(msg.get("cmd"), str) or not msg["cmd"]:
            return self._encode(
                {
                    "ok": False,
                    "error": {
                        "code": "INVALID_REQUEST",
                        "message": "Request field 'cmd' must be a non-empty string",
                    },
                },
                request_id,
            )

        args = msg.get("args", {})
        if not isinstance(args, dict):
            return self._encode(
                {
                    "ok": False,
                    "error": {
                        "code": "INVALID_REQUEST",
                        "message": "Request field 'args' must be an object",
                    },
                },
                request_id,
            )

        try:
            return self._encode({"ok": True, "result": self._route(msg["cmd"], args)}, request_id)
        except ReferenceError as exc:
            return self._encode(
                {"ok": False, "error": {"code": exc.code, "message": str(exc)}}, request_id
            )
        except Exception as exc:  # noqa: BLE001 — never drop a connection
            logger.exception("Unhandled error in command '%s'", msg.get("cmd"))
            return self._encode(
                {"ok": False, "error": {"code": "ENGINE_ERROR", "message": str(exc)}},
                request_id,
            )

    def _route(self, cmd: str, args: dict[str, Any]) -> Any:
        runtime = self._runtime
        # Every verb advertised in hello() has a handler here, and nothing
        # else does — capability honesty is what the TCK checks first.
        handlers = {
            "hello": lambda: runtime.hello(),
            "ping": lambda: runtime.ping(),
            "simulate": lambda: runtime.simulate(args),
            "shutdown": lambda: self._shutdown_engine(),
            "simulate_start": lambda: runtime.simulate_start(args),
            "simulate_status": lambda: runtime.simulate_status(args),
            "simulate_stop": lambda: runtime.simulate_stop(args),
            "teleop_start": lambda: runtime.teleop_start(args),
            "teleop_command": lambda: runtime.teleop_command(args),
            "teleop_state": lambda: runtime.teleop_state(args),
            "teleop_stop": lambda: runtime.teleop_stop(args),
            "diagnose": lambda: runtime.diagnose(args),
        }
        handler = handlers.get(cmd)
        if handler is None:
            raise ReferenceError(f"Unknown command: {cmd}", code="UNSUPPORTED_COMMAND")
        return handler()

    def _shutdown_engine(self) -> dict[str, Any]:
        result = self._runtime.shutdown()
        self._stop_event.set()
        return result


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="SolidMind reference engine")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9880)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    server = ReferenceBridgeServer(host=args.host, port=args.port)

    def _shutdown(signum: int, _frame: Any) -> None:
        logger.info("Received signal %d, shutting down", signum)
        server.shutdown()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    server.serve_forever()


if __name__ == "__main__":
    main()
