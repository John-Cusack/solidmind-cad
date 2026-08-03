"""Tests for ``isaac_bridge.client`` — the bridge's own contract client.

The bridge ships this client so ``isaac_bridge.lifecycle`` (and the scripts and
tests built on it) never import core.  It runs against a real ``BridgeServer``
in reference mode — no Isaac Sim required.
"""

from __future__ import annotations

import socket
import threading
import time
import unittest

from isaac_bridge.bridge_server import BridgeServer
from isaac_bridge.client import BridgeClient, BridgeCommandError, BridgeConnectionError


def _unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _LiveBridge:
    """Run a BridgeServer in a daemon thread for the duration of a test."""

    def __init__(self) -> None:
        self.port = _unused_port()
        self._server = BridgeServer(host="127.0.0.1", port=self.port, headless=True)
        self._thread: threading.Thread | None = None

    def __enter__(self) -> _LiveBridge:
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    return self
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("bridge did not start")

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        if self._thread is not None:
            self._thread.join(timeout=5.0)


class TestBridgeClient(unittest.TestCase):
    def test_hello_and_ping(self) -> None:
        with _LiveBridge() as bridge:
            client = BridgeClient(port=bridge.port)
            client.connect(timeout=2.0)
            try:
                hello = client.hello()
                self.assertEqual(hello["engine"], "isaac")
                self.assertEqual(hello["protocol_version"], "1.0.0")
                self.assertTrue(client.ping())
            finally:
                client.disconnect()
            self.assertFalse(client.is_connected)

    def test_request_id_is_passed_through(self) -> None:
        with _LiveBridge() as bridge:
            client = BridgeClient(port=bridge.port)
            client.connect(timeout=2.0)
            try:
                # The client returns only the result payload; a mismatched echo
                # would surface as a protocol failure on the bridge side.
                self.assertTrue(client.send_command("ping", request_id="rid-1")["pong"])
            finally:
                client.disconnect()

    def test_command_error_carries_contract_code(self) -> None:
        with _LiveBridge() as bridge:
            client = BridgeClient(port=bridge.port)
            client.connect(timeout=2.0)
            try:
                with self.assertRaises(BridgeCommandError) as ctx:
                    client.send_command("definitely_not_a_verb")
            finally:
                client.disconnect()
        self.assertEqual(ctx.exception.code, "UNSUPPORTED_COMMAND")

    def test_connect_refused(self) -> None:
        client = BridgeClient(port=_unused_port())
        with self.assertRaises(BridgeConnectionError):
            client.connect(timeout=0.5)

    def test_send_without_connect(self) -> None:
        client = BridgeClient(port=_unused_port())
        with self.assertRaises(BridgeConnectionError):
            client.send_command("ping")

    def test_connect_with_retry_gives_up(self) -> None:
        client = BridgeClient(port=_unused_port())
        with self.assertRaises(BridgeConnectionError):
            client.connect_with_retry(max_retries=2, retry_delay=0.05)


if __name__ == "__main__":
    unittest.main()
