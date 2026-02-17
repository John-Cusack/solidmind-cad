"""TCP socket server for the SolidMind FreeCAD addon.

Listens on ``localhost:9876`` (configurable) for newline-delimited JSON
commands from the MCP bridge process.  Each command is dispatched to the
appropriate handler in ``commands.py`` and the result is sent back.

The server runs in a background thread so it does not block FreeCAD's GUI
event loop.  Commands are dispatched to the **main thread** via a QTimer
because FreeCAD's Python API is not thread-safe.
"""
from __future__ import annotations

import inspect
import json
import logging
import queue
import socket
import threading
import traceback
from typing import Any, Callable

from freecad_addon.commands import COMMAND_HANDLERS
from freecad_addon.protocol import Command, Response, encode_message

logger = logging.getLogger("solidmind.socket_server")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9876


def _fc_log(msg: str, *, error: bool = False) -> None:
    """Log to both Python logging and FreeCAD console."""
    try:
        import FreeCAD
        if error:
            FreeCAD.Console.PrintError(f"[SolidMind] {msg}\n")
        else:
            FreeCAD.Console.PrintMessage(f"[SolidMind] {msg}\n")
    except Exception:
        pass
    if error:
        logger.error(msg)
    else:
        logger.info(msg)


def _filter_kwargs(handler: Callable[..., Any], args: dict[str, Any]) -> dict[str, Any]:
    """Filter *args* to only keys the handler accepts.

    This provides forward-compatibility: a newer bridge can send kwargs
    that an older addon handler doesn't know about yet, and they'll be
    silently dropped instead of raising TypeError.
    """
    sig = inspect.signature(handler)
    # If the handler accepts **kwargs, pass everything through.
    if any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    ):
        return args
    accepted = {
        name
        for name, p in sig.parameters.items()
        if p.kind
        in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        )
    }
    return {k: v for k, v in args.items() if k in accepted}


_DEFAULT_TIMEOUT = 30.0
_LONG_TIMEOUT_COMMANDS = frozenset({"export", "sketch_populate"})


def _command_timeout(cmd: str, args: dict[str, Any]) -> float:
    """Compute an adaptive timeout for a command.

    ``sketch_populate`` scales with element+constraint count.
    ``export`` gets a flat 120s (STL tessellation of complex geometry).
    Everything else uses the default 30s.
    """
    if cmd == "sketch_populate":
        n_elements = len(args.get("elements", []))
        n_constraints = len(args.get("constraints", []))
        # Base 30s + 0.1s per item, minimum 30s
        return max(_DEFAULT_TIMEOUT, 30.0 + (n_elements + n_constraints) * 0.1)
    if cmd == "export":
        return 120.0
    if cmd in _LONG_TIMEOUT_COMMANDS:
        return 120.0
    return _DEFAULT_TIMEOUT


class _MainThreadJob:
    """A command to be executed on the main thread."""

    __slots__ = ("handler", "args", "event", "response")

    def __init__(self, handler: Callable[..., Any], args: dict[str, Any]) -> None:
        self.handler = handler
        self.args = args
        self.event = threading.Event()
        self.response: Response | None = None


class AddonSocketServer:
    """TCP server that accepts connections and dispatches JSON commands."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        handlers: dict[str, Callable[..., Any]] | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._handlers = handlers or COMMAND_HANDLERS
        self._server_socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._job_queue: queue.Queue[_MainThreadJob] = queue.Queue()
        self._timer: Any = None  # QTimer reference

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> None:
        """Start the server in a background thread."""
        if self._running:
            _fc_log(f"Server already running on {self._host}:{self._port}")
            return

        self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_socket.settimeout(1.0)  # So we can check _running periodically
        self._server_socket.bind((self._host, self._port))
        self._server_socket.listen(1)

        self._running = True
        self._thread = threading.Thread(target=self._accept_loop, daemon=True, name="solidmind-server")
        self._thread.start()

        # Start a QTimer on the main thread to poll the job queue.
        self._start_main_thread_timer()

        _fc_log(f"Server started on {self._host}:{self._port}")

    def _start_main_thread_timer(self) -> None:
        """Set up a QTimer to process jobs on the main GUI thread."""
        try:
            from freecad_addon.qt_compat import QTimer
        except ImportError:
            _fc_log(
                "No PySide found — commands will run on background thread (unsafe)",
                error=True,
            )
            return

        self._timer = QTimer()
        self._timer.setInterval(10)  # 10ms polling
        self._timer.timeout.connect(self._process_jobs)
        self._timer.start()

    def _process_jobs(self) -> None:
        """Run on main thread via QTimer — execute queued jobs."""
        while not self._job_queue.empty():
            try:
                job = self._job_queue.get_nowait()
            except queue.Empty:
                break
            try:
                result = job.handler(**job.args)
                job.response = Response.success(result)
            except Exception as e:
                tb = traceback.format_exc()
                _fc_log(f"Command failed: {e}\n{tb}", error=True)
                job.response = Response.failure(f"{type(e).__name__}: {e}")
            finally:
                job.event.set()

    def stop(self) -> None:
        """Stop the server and close all connections."""
        self._running = False
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        if self._server_socket:
            try:
                self._server_socket.close()
            except OSError:
                pass
            self._server_socket = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        self._thread = None
        _fc_log("Server stopped")

    def _accept_loop(self) -> None:
        """Accept connections in the background thread."""
        while self._running:
            try:
                assert self._server_socket is not None
                conn, addr = self._server_socket.accept()
                _fc_log(f"Client connected from {addr[0]}:{addr[1]}")
                client_thread = threading.Thread(
                    target=self._handle_client,
                    args=(conn, addr),
                    daemon=True,
                    name=f"solidmind-client-{addr[1]}",
                )
                client_thread.start()
            except socket.timeout:
                continue
            except OSError:
                if self._running:
                    logger.exception("Accept error")
                break

    def _handle_client(self, conn: socket.socket, addr: tuple[str, int]) -> None:
        """Handle a single client connection."""
        buffer = b""
        try:
            conn.settimeout(None)  # Blocking reads
            while self._running:
                data = conn.recv(4096)
                if not data:
                    break
                buffer += data

                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line.strip():
                        continue
                    response = self._dispatch(line)
                    conn.sendall(encode_message(response))
        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception:
            logger.exception("Error handling client %s:%d", *addr)
        finally:
            try:
                conn.close()
            except OSError:
                pass
            _fc_log(f"Client disconnected: {addr[0]}:{addr[1]}")

    def _dispatch(self, line: bytes) -> Response:
        """Parse a command line, execute the handler, return a response."""
        try:
            cmd = Command.from_json(line.decode("utf-8"))
        except (json.JSONDecodeError, KeyError, UnicodeDecodeError) as e:
            return Response.failure(f"Invalid command: {e}")

        _fc_log(f"Command: {cmd.cmd} {cmd.args}")

        # Special built-in commands (safe from any thread)
        if cmd.cmd == "ping":
            return Response.success({"pong": True})

        handler = self._handlers.get(cmd.cmd)
        if handler is None:
            return Response.failure(f"Unknown command: {cmd.cmd}")

        # Filter out kwargs the handler doesn't accept (forward-compatibility).
        args = _filter_kwargs(handler, cmd.args)

        # If we have a QTimer, dispatch to main thread; otherwise run directly.
        if self._timer is not None:
            job = _MainThreadJob(handler, args)
            self._job_queue.put(job)
            # Adaptive timeout: longer for commands that may process many items
            timeout = _command_timeout(cmd.cmd, args)
            job.event.wait(timeout=timeout)
            if job.response is None:
                return Response.failure(
                    f"Command timed out after {timeout:.0f}s (main thread busy)"
                )
            return job.response
        else:
            try:
                result = handler(**args)
                return Response.success(result)
            except Exception as e:
                tb = traceback.format_exc()
                _fc_log(f"Command {cmd.cmd} failed: {e}\n{tb}", error=True)
                return Response.failure(f"{type(e).__name__}: {e}")


# Module-level singleton
_server: AddonSocketServer | None = None


def get_server() -> AddonSocketServer:
    """Get or create the global server instance."""
    global _server
    if _server is None:
        _server = AddonSocketServer()
    return _server


def start_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> AddonSocketServer:
    """Create and start the server."""
    global _server
    _server = AddonSocketServer(host=host, port=port)
    _server.start()
    return _server


def stop_server() -> None:
    """Stop the global server if running."""
    global _server
    if _server is not None:
        _server.stop()
        _server = None
