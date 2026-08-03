"""One client for every engine.

Three near-identical clients (Isaac, Gazebo, Chrono, ~420 lines each) were the
standing proof that the protocol is uniform.  They collapse into this one:
the contract is the same envelope for every engine, so the client is too, and
adding an engine adds no client code at all.

Two things live here that per-engine clients could not have:

* **Capability caching.** ``hello`` is asked once per connection and cached, so
  callers can gate on what an engine actually advertises rather than on its
  name.
* **The msg-rate tripwire** (architecture doc §3.7).  Core must never end up
  inside a physics-rate loop; a sustained >100 msg/s on any command logs a
  warning, turning the one-time tight-loop audit into a permanent check.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from collections import deque
from typing import Any

from server.engine_registry import resolve_host, resolve_port
from server.jsonutil import dumps as json_dumps
from server.jsonutil import loads as json_loads

logger = logging.getLogger("solidmind.engine_client")

CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 120.0
MAX_RETRIES = 3
RETRY_DELAY = 1.0

#: Sustained rate above which core is presumed to be inside a control loop.
TRIPWIRE_MSG_PER_S = 100.0
#: Window the rate is measured over.
_TRIPWIRE_WINDOW_S = 1.0
#: Don't re-warn about the same command more often than this.
_TRIPWIRE_COOLDOWN_S = 60.0


class EngineConnectionError(Exception):
    """Raised when the engine cannot be reached."""


class EngineCommandError(Exception):
    """Raised when an engine answers ``ok: false``."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class _RateTripwire:
    """Per-command message-rate monitor (architecture doc, Principle 5)."""

    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = {}
        self._last_warned: dict[str, float] = {}
        self._lock = threading.Lock()

    def record(self, engine: str, cmd: str) -> float:
        """Record one message and return the current rate for *cmd*."""
        now = time.monotonic()
        key = f"{engine}.{cmd}"
        with self._lock:
            events = self._events.setdefault(key, deque())
            events.append(now)
            cutoff = now - _TRIPWIRE_WINDOW_S
            while events and events[0] < cutoff:
                events.popleft()
            rate = len(events) / _TRIPWIRE_WINDOW_S
            if rate > TRIPWIRE_MSG_PER_S:
                last = self._last_warned.get(key, 0.0)
                if now - last > _TRIPWIRE_COOLDOWN_S:
                    self._last_warned[key] = now
                    logger.warning(
                        "Message-rate tripwire: %s is running at %.0f msg/s (>%.0f). "
                        "Core must not sit inside a control loop — move the loop "
                        "engine-side (controller spec, policy, or profile).",
                        key,
                        rate,
                        TRIPWIRE_MSG_PER_S,
                    )
            return rate

    def rate(self, engine: str, cmd: str) -> float:
        with self._lock:
            events = self._events.get(f"{engine}.{cmd}")
            if not events:
                return 0.0
            cutoff = time.monotonic() - _TRIPWIRE_WINDOW_S
            return len([e for e in events if e >= cutoff]) / _TRIPWIRE_WINDOW_S

    def reset(self) -> None:
        with self._lock:
            self._events.clear()
            self._last_warned.clear()


tripwire = _RateTripwire()


class EngineClient:
    """NDJSON/TCP client speaking Engine Integration Contract v1."""

    def __init__(
        self,
        engine: str,
        *,
        host: str | None = None,
        port: int | None = None,
        connect_timeout: float = CONNECT_TIMEOUT,
        read_timeout: float = READ_TIMEOUT,
    ) -> None:
        self.engine = str(engine).strip().lower()
        self._host = host or resolve_host(self.engine)
        self._port = port if port is not None else resolve_port(self.engine)
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._sock: socket.socket | None = None
        self._buffer = b""
        self._hello: dict[str, Any] | None = None

    # -- connection -----------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._sock is not None

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    @property
    def address(self) -> str:
        return f"{self._host}:{self._port}"

    def connect(self, timeout: float | None = None) -> None:
        if self._sock is not None:
            return
        effective = self._connect_timeout if timeout is None else timeout
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(effective)
        try:
            sock.connect((self._host, self._port))
        except (ConnectionRefusedError, OSError) as exc:
            try:
                sock.close()
            except OSError:
                pass
            raise EngineConnectionError(
                f"Cannot connect to the {self.engine} engine at {self.address}: {exc}. "
                f"Start it with sim.start_engine('{self.engine}')."
            ) from exc
        self._sock = sock
        self._buffer = b""
        self._hello = None
        logger.info("Connected to %s engine at %s", self.engine, self.address)

    def connect_with_retry(
        self,
        max_retries: int = MAX_RETRIES,
        retry_delay: float = RETRY_DELAY,
    ) -> None:
        last_error: Exception | None = None
        for attempt in range(max_retries):
            try:
                self.connect()
                return
            except EngineConnectionError as exc:
                last_error = exc
                if attempt < max_retries - 1:
                    time.sleep(retry_delay * (2**attempt))
        raise EngineConnectionError(
            f"Failed to connect to the {self.engine} engine after {max_retries} attempts."
        ) from last_error

    def disconnect(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
            self._buffer = b""
            self._hello = None

    # -- contract verbs -------------------------------------------------

    def hello(self, *, refresh: bool = False) -> dict[str, Any]:
        """Capability handshake, cached per connection (contract §2)."""
        if self._hello is None or refresh:
            result = self.send_command("hello", timeout=10.0)
            self._hello = result if isinstance(result, dict) else {}
        return self._hello

    def capabilities(self) -> dict[str, Any]:
        """The engine's advertised capabilities, or ``{}`` if it can't say."""
        try:
            return dict(self.hello().get("capabilities", {}))
        except (EngineConnectionError, EngineCommandError):
            return {}

    def supports_mode(self, mode: str) -> bool:
        return mode in (self.capabilities().get("modes") or [])

    def supports_feature(self, feature: str) -> bool:
        return feature in (self.capabilities().get("features") or [])

    def supports_format(self, fmt: str) -> bool:
        return fmt in (self.capabilities().get("formats") or [])

    def ping(self) -> bool:
        try:
            result = self.send_command("ping", timeout=5.0)
        except Exception:
            return False
        return bool(isinstance(result, dict) and result.get("pong", False))

    def shutdown(self) -> dict[str, Any]:
        return self.send_command("shutdown", timeout=10.0)

    def simulate(self, *, duration_s: float = 1.0, **args: Any) -> dict[str, Any]:
        """Batch simulation, with a read timeout scaled to the run length."""
        return self.send_command(
            "simulate",
            timeout=max(self._read_timeout, duration_s * 100),
            duration_s=duration_s,
            **args,
        )

    def send_command(
        self,
        cmd: str,
        timeout: float | None = None,
        request_id: str | None = None,
        **args: Any,
    ) -> Any:
        """Send one request and return its ``result`` payload."""
        self._ensure_connected()
        assert self._sock is not None
        effective = self._read_timeout if timeout is None else timeout

        tripwire.record(self.engine, cmd)

        envelope: dict[str, Any] = {"cmd": cmd, "args": args}
        if request_id is not None:
            envelope["request_id"] = request_id
        raw = json_dumps(envelope) + b"\n"
        try:
            self._sock.sendall(raw)
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            self._sock = None
            raise EngineConnectionError(
                f"Connection to {self.engine} lost while sending '{cmd}': {exc}"
            ) from exc

        response = self._read_response(effective)
        if not isinstance(response, dict):
            raise EngineCommandError(
                f"Malformed response for '{cmd}': expected an object",
                code="INVALID_REQUEST",
            )
        if not response.get("ok", False):
            error = response.get("error", "Unknown error")
            if isinstance(error, dict):
                code = error.get("code") if isinstance(error.get("code"), str) else None
                message = error.get("message") or str(error)
            else:
                code, message = None, str(error)
            logger.error("%s command '%s' failed: %s (code=%s)", self.engine, cmd, message, code)
            raise EngineCommandError(str(message), code=code)
        return response.get("result")

    # -- framing --------------------------------------------------------

    def _ensure_connected(self) -> None:
        if self._sock is None:
            self.connect()

    def _read_response(self, timeout: float) -> Any:
        assert self._sock is not None
        self._sock.settimeout(timeout)
        while b"\n" not in self._buffer:
            try:
                data = self._sock.recv(65536)
            except TimeoutError as exc:
                raise EngineConnectionError(
                    f"Timed out waiting for the {self.engine} engine ({timeout}s)"
                ) from exc
            except (ConnectionResetError, OSError) as exc:
                self._sock = None
                raise EngineConnectionError(
                    f"Connection to {self.engine} lost while reading: {exc}"
                ) from exc
            if not data:
                self._sock = None
                raise EngineConnectionError(f"The {self.engine} engine closed the connection")
            self._buffer += data

        line, self._buffer = self._buffer.split(b"\n", 1)
        return json_loads(line)


# ---------------------------------------------------------------------------
# Per-engine singletons
# ---------------------------------------------------------------------------

_clients: dict[str, EngineClient] = {}
_clients_lock = threading.Lock()


def get_client(engine: str, *, connect: bool = True) -> EngineClient | None:
    """Return a connected client for *engine*, or None if it isn't running.

    Connection failures are not errors here: an engine that isn't running is
    the normal case, and callers turn that into a "start it with…" message.
    """
    name = str(engine).strip().lower()
    with _clients_lock:
        client = _clients.get(name)
        if client is None:
            client = EngineClient(name)
            _clients[name] = client

    if not connect:
        return client
    if client.is_connected:
        return client
    try:
        client.connect()
    except EngineConnectionError as exc:
        logger.debug("Engine %s unavailable: %s", name, exc)
        return None
    return client


def reset_client(engine: str | None = None) -> None:
    """Drop cached clients so the next call reconnects (used after restarts)."""
    with _clients_lock:
        names = [engine.strip().lower()] if engine else list(_clients)
        for name in names:
            client = _clients.pop(name, None)
            if client is not None:
                client.disconnect()


def engine_available(engine: str, *, timeout: float = 1.0) -> bool:
    """Cheap liveness probe that never leaves a client behind."""
    host, port = resolve_host(engine), resolve_port(engine)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect((host, port))
        return True
    except (ConnectionRefusedError, OSError, TimeoutError):
        return False
    finally:
        sock.close()
