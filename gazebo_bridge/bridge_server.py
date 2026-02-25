"""TCP bridge server for Gazebo simulation/teleop commands.

Simpler than the Isaac bridge — no main-thread pump needed.
Gazebo handles its own event loop internally.
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import socket
import threading
from typing import Any

from gazebo_bridge.runtime_gazebo import GazeboRuntime, GazeboRuntimeError

logger = logging.getLogger("solidmind.gazebo_bridge")


class GazeboBridgeServer:
    """Newline-delimited JSON TCP server for Gazebo command handling."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 9879,
    ) -> None:
        self._host = host
        self._port = port
        self._runtime = GazeboRuntime()
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
            "Gazebo bridge listening on %s:%d",
            self._host,
            self._port,
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

    def _handle_connection(self, conn: socket.socket, addr: tuple[str, int]) -> None:
        logger.info("New connection from %s:%d", *addr)
        buf = b""
        try:
            conn.settimeout(0.5)
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
                    if not line:
                        continue
                    response = self._dispatch(line)
                    conn.sendall(response.encode("utf-8"))
        except Exception:
            logger.exception("Error handling connection from %s:%d", *addr)
        finally:
            try:
                conn.close()
            except OSError:
                pass
            logger.info("Connection from %s:%d closed", *addr)

    def _dispatch(self, line: bytes) -> str:
        try:
            msg = json.loads(line.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            return json.dumps({"ok": False, "error": f"JSON parse error: {exc}"}) + "\n"

        cmd = msg.get("cmd", "")
        args = msg.get("args", {})

        try:
            result = self._route(cmd, args)
            return json.dumps({"ok": True, "result": result}) + "\n"
        except GazeboRuntimeError as exc:
            return json.dumps({
                "ok": False,
                "error": {"code": exc.code or "GAZEBO_COMMAND_ERROR", "message": str(exc)},
            }) + "\n"
        except Exception as exc:
            logger.exception("Unhandled error in command '%s'", cmd)
            return json.dumps({
                "ok": False,
                "error": {"code": "GAZEBO_INTERNAL_ERROR", "message": str(exc)},
            }) + "\n"

    def _route(self, cmd: str, args: dict[str, Any]) -> Any:
        if cmd == "ping":
            return self._runtime.handle_ping()
        if cmd == "simulate":
            return self._runtime.handle_simulate(args)
        if cmd == "teleop_start":
            return self._runtime.handle_teleop_start(args)
        if cmd == "teleop_command":
            return self._runtime.handle_teleop_command(args)
        if cmd == "teleop_state":
            return self._runtime.handle_teleop_state(args)
        if cmd == "teleop_stop":
            return self._runtime.handle_teleop_stop(args)
        raise GazeboRuntimeError(f"Unknown command: {cmd}", code="UNKNOWN_COMMAND")


def main() -> None:
    parser = argparse.ArgumentParser(description="SolidMind Gazebo Bridge")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9879)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    server = GazeboBridgeServer(host=args.host, port=args.port)

    def _shutdown(signum: int, frame: Any) -> None:
        logger.info("Received signal %d, shutting down", signum)
        server.shutdown()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    server.serve_forever()


if __name__ == "__main__":
    main()
