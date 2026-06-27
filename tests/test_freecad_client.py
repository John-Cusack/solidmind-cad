"""Tests for the MCP bridge FreeCAD client."""
from __future__ import annotations

import json
import socket
import threading
import unittest

from server.freecad_client import FreeCADClient, FreeCADCommandError, FreeCADConnectionError


def _make_echo_server(host: str, port: int) -> tuple[socket.socket, threading.Event]:
    """Create a simple echo server that responds to commands."""
    ready = threading.Event()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.settimeout(5.0)
    srv.bind((host, port))
    srv.listen(1)

    def run() -> None:
        ready.set()
        try:
            conn, _ = srv.accept()
            conn.settimeout(5.0)
            buf = b""
            while True:
                data = conn.recv(4096)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    cmd = json.loads(line)
                    if cmd["cmd"] == "ping":
                        resp = {"ok": True, "result": {"pong": True}}
                    elif cmd["cmd"] == "fail":
                        resp = {"ok": False, "error": "intentional failure"}
                    else:
                        resp = {"ok": True, "result": {"echo": cmd}}
                    conn.sendall((json.dumps(resp) + "\n").encode())
            conn.close()
        except (TimeoutError, OSError):
            pass
        finally:
            srv.close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    ready.wait(timeout=2.0)
    return srv, ready


class TestFreeCADClientConnection(unittest.TestCase):
    def test_connection_refused(self) -> None:
        client = FreeCADClient(host="127.0.0.1", port=19999)
        with self.assertRaises(FreeCADConnectionError):
            client.connect(timeout=0.5)

    def test_connect_and_ping(self) -> None:
        srv, _ = _make_echo_server("127.0.0.1", 19876)
        try:
            client = FreeCADClient(host="127.0.0.1", port=19876)
            client.connect(timeout=2.0)
            self.assertTrue(client.is_connected)
            self.assertTrue(client.ping())
            client.disconnect()
            self.assertFalse(client.is_connected)
        finally:
            srv.close()

    def test_send_command_success(self) -> None:
        srv, _ = _make_echo_server("127.0.0.1", 19877)
        try:
            client = FreeCADClient(host="127.0.0.1", port=19877)
            client.connect(timeout=2.0)
            result = client.send_command("test_cmd", x=1, y=2)
            self.assertIn("echo", result)
            self.assertEqual(result["echo"]["cmd"], "test_cmd")
            client.disconnect()
        finally:
            srv.close()

    def test_send_command_failure(self) -> None:
        srv, _ = _make_echo_server("127.0.0.1", 19878)
        try:
            client = FreeCADClient(host="127.0.0.1", port=19878)
            client.connect(timeout=2.0)
            with self.assertRaises(FreeCADCommandError):
                client.send_command("fail")
            client.disconnect()
        finally:
            srv.close()


class TestFreeCADClientRetry(unittest.TestCase):
    def test_connect_with_retry_fails(self) -> None:
        client = FreeCADClient(host="127.0.0.1", port=19999)
        with self.assertRaises(FreeCADConnectionError):
            client.connect_with_retry(max_retries=2, retry_delay=0.1)


if __name__ == "__main__":
    unittest.main()
