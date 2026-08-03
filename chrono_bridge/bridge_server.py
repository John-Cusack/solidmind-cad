"""Contract face for the Chrono engine.

Core sends a neutral mechanism; this shim compiles it into Chrono's native
simulation spec (``chrono_bridge/spec_builder.py``), forwards that to the C++
daemon, and post-processes the result — derived planet speeds included — back
into contract shape.

Why a shim at all: the daemon is C++ and its vocabulary is Chrono's, so the
translation has to live *somewhere* on the engine side.  Putting it in a
Python front-end keeps the C++ free of domain logic and gives engine-chrono
the same shape as every other engine repo — contract on the front, native on
the back (architecture doc §10, recorded decisions).

Run it with::

    python3 -m chrono_bridge.bridge_server --port 9877 \
        --launch-daemon chrono_daemon/run.sh

which starts the C++ daemon underneath it and stops it again on shutdown.
Point it at an already-running daemon instead with ``--daemon-port``.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from chrono_bridge.daemon_client import (
    DEFAULT_DAEMON_HOST,
    DEFAULT_DAEMON_PORT,
    DaemonClient,
    DaemonError,
)

logger = logging.getLogger("solidmind.chrono_bridge")

# Engine Integration Contract v1 (docs/engine-contract.md).  Duplicated per
# bridge on purpose — the contract is data, not a shared import.
_PROTOCOL_VERSION = "1.0.0"
_CONTRACT_VERSIONS_SUPPORTED = ("1",)
_BRIDGE_VERSION = "1.0.0"

_CAPABILITIES: dict[str, Any] = {
    "modes": ["batch"],  # simulate only — no sessions, no teleop
    "formats": ["mechanism"],  # the canonical mechanism; compiled here
    "features": [],
    "fields": {"emits": [], "accepts": []},
}


class ChronoBridgeError(Exception):
    """Raised for contract-level failures."""

    def __init__(self, message: str, *, code: str = "ENGINE_ERROR") -> None:
        super().__init__(message)
        self.code = code


class ChronoBridgeServer:
    """Newline-delimited JSON TCP server in front of the Chrono daemon."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 9877,
        daemon_host: str = DEFAULT_DAEMON_HOST,
        daemon_port: int = DEFAULT_DAEMON_PORT,
        daemon_command: list[str] | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._daemon = DaemonClient(daemon_host, daemon_port)
        self._daemon_command = daemon_command
        self._daemon_proc: subprocess.Popen[bytes] | None = None
        self._sock: socket.socket | None = None
        self._stop_event = threading.Event()

    def start_daemon(self, timeout_s: float = 30.0) -> None:
        """Spawn the C++ daemon as a child, when this shim owns it.

        Running the daemon under the shim keeps the pair a single supervised
        process for whoever launched us — stop the shim and the daemon goes
        with it.
        """
        if self._daemon_command is None:
            return
        logger.info("Starting Chrono daemon: %s", " ".join(self._daemon_command))
        self._daemon_proc = subprocess.Popen(self._daemon_command)  # noqa: S603
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._daemon.ping():
                logger.info("Chrono daemon ready at %s", self._daemon.address)
                return
            if self._daemon_proc.poll() is not None:
                raise ChronoBridgeError(
                    f"Chrono daemon exited immediately (rc={self._daemon_proc.returncode})"
                )
            time.sleep(0.1)
        raise ChronoBridgeError(f"Chrono daemon did not become ready within {timeout_s}s")

    def _stop_daemon_process(self) -> None:
        if self._daemon_proc is None or self._daemon_proc.poll() is not None:
            return
        self._daemon_proc.terminate()
        try:
            self._daemon_proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            self._daemon_proc.kill()

    @property
    def port(self) -> int:
        return self._port

    # -- lifecycle ------------------------------------------------------

    def serve_forever(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.settimeout(0.2)
        srv.bind((self._host, self._port))
        srv.listen(8)
        self._sock = srv
        self._port = int(srv.getsockname()[1])
        logger.info(
            "Chrono bridge listening on %s:%d (daemon at %s)",
            self._host,
            self._port,
            self._daemon.address,
        )
        try:
            while not self._stop_event.is_set():
                try:
                    conn, addr = srv.accept()
                except TimeoutError:
                    continue
                except OSError:
                    break
                threading.Thread(
                    target=self._handle_connection,
                    args=(conn, addr),
                    daemon=True,
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
        self._stop_daemon_process()

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

    # -- protocol -------------------------------------------------------

    def _encode(self, payload: dict[str, Any], request_id: str | None) -> str:
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

        if not isinstance(msg, dict) or not isinstance(msg.get("cmd"), str):
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
        except ChronoBridgeError as exc:
            return self._encode(
                {"ok": False, "error": {"code": exc.code, "message": str(exc)}}, request_id
            )
        except DaemonError as exc:
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
        if cmd == "hello":
            return self._hello()
        if cmd == "ping":
            return {"pong": True, "daemon_reachable": self._daemon.ping()}
        if cmd == "simulate":
            return self._simulate(args)
        if cmd == "shutdown":
            return self._shutdown_engine()
        raise ChronoBridgeError(f"Unknown command: {cmd}", code="UNSUPPORTED_COMMAND")

    def _hello(self) -> dict[str, Any]:
        """Capability handshake — Engine Integration Contract v1 §2."""
        return {
            "protocol_version": _PROTOCOL_VERSION,
            "contract_versions_supported": list(_CONTRACT_VERSIONS_SUPPORTED),
            "engine": "chrono",
            "engine_version": self._daemon.version() or _BRIDGE_VERSION,
            "runtime_mode": "real",
            "capabilities": {
                "modes": list(_CAPABILITIES["modes"]),
                "formats": list(_CAPABILITIES["formats"]),
                "features": list(_CAPABILITIES["features"]),
                "fields": dict(_CAPABILITIES["fields"]),
            },
        }

    def _simulate(self, args: dict[str, Any]) -> dict[str, Any]:
        """Compile the mechanism, run it on the daemon, return contract results."""
        from chrono_bridge.spec_builder import (
            add_derived_speeds,
            build_simulation_spec,
            validate_simulation_spec,
        )

        mechanism = args.get("mechanism")
        if not isinstance(mechanism, dict):
            raise ChronoBridgeError(
                "simulate requires a 'mechanism' object",
                code="INVALID_REQUEST",
            )

        duration_s = float(args.get("duration_s", 1.0))
        dt_s = float(args.get("dt_s", 0.001))
        output_interval = float(args.get("output_interval", 0.01))

        spec = build_simulation_spec(mechanism)
        issues = validate_simulation_spec(spec)
        if issues:
            raise ChronoBridgeError(
                "Simulation spec failed pre-flight validation:\n"
                + "\n".join(f"  - {issue}" for issue in issues),
                code="PACKAGE_INVALID",
            )

        result = self._daemon.send(
            "simulate",
            {
                "simulation_spec": spec,
                "duration_s": duration_s,
                "dt_s": dt_s,
                "output_interval": output_interval,
            },
        )
        if not isinstance(result, dict):
            raise ChronoBridgeError("Daemon returned a non-object simulate result")

        # Planet speeds are derived from sun + carrier via Willis kinematics;
        # that is part of simulating a planetary set correctly, so it happens
        # here rather than leaking the spec back to core.
        add_derived_speeds(result, spec)

        summary = result.setdefault("summary", {})
        summary.setdefault("dt_s", dt_s)
        summary.setdefault("simulation_time_s", duration_s)
        summary.setdefault("engine_mode", "chrono")
        return result

    def _shutdown_engine(self) -> dict[str, Any]:
        """Drain: stop the daemon, then this process."""
        daemon_stopped = True
        try:
            self._daemon.send("shutdown")
        except DaemonError as exc:
            daemon_stopped = False
            logger.warning("Daemon shutdown failed: %s", exc)
        self._stop_daemon_process()
        self._stop_event.set()
        return {"message": "Shutting down", "daemon_stopped": daemon_stopped}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="SolidMind Chrono bridge (contract shim)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9877, help="Contract port core connects to")
    parser.add_argument("--daemon-host", default=DEFAULT_DAEMON_HOST)
    parser.add_argument(
        "--daemon-port",
        type=int,
        default=DEFAULT_DAEMON_PORT,
        help=f"Port the C++ chrono_daemon listens on (default {DEFAULT_DAEMON_PORT})",
    )
    parser.add_argument(
        "--launch-daemon",
        default=None,
        help=(
            "Path to the chrono_daemon binary (or run.sh). When given, the shim "
            "starts it on --daemon-port and stops it on shutdown, so the pair is "
            "one supervised process."
        ),
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    daemon_command: list[str] | None = None
    if args.launch_daemon:
        launcher = Path(args.launch_daemon)
        if not launcher.is_file():
            parser.error(f"--launch-daemon path does not exist: {launcher}")
        daemon_command = (
            ["bash", str(launcher)] if launcher.suffix == ".sh" else [str(launcher)]
        ) + ["--port", str(args.daemon_port)]

    server = ChronoBridgeServer(
        host=args.host,
        port=args.port,
        daemon_host=args.daemon_host,
        daemon_port=args.daemon_port,
        daemon_command=daemon_command,
    )

    def _shutdown(signum: int, _frame: Any) -> None:
        logger.info("Received signal %d, shutting down", signum)
        server.shutdown()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        server.start_daemon()
    except ChronoBridgeError as exc:
        logger.critical("%s", exc)
        raise SystemExit(1) from exc

    server.serve_forever()


if __name__ == "__main__":
    main()
