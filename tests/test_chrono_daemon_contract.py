"""Contract handshake against the real Chrono daemon.

The per-engine clients are gone (one generic client now lives in
``server/engine_client.py``); what remains here is the C++ daemon's own
conformance, which needs the built binary.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import unittest
from pathlib import Path

_DAEMON_PATH = Path(__file__).resolve().parents[1] / "chrono_daemon" / "build" / "chrono_daemon"
_CHRONO_LIB_DIR = os.environ.get("CHRONO_LIB_DIR", "/usr/local/lib")


def _daemon_available() -> bool:
    return _DAEMON_PATH.is_file() and os.access(_DAEMON_PATH, os.X_OK)


def _wait_for_listening(host: str, port: int, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            try:
                s.connect((host, port))
                return True
            except OSError:
                time.sleep(0.05)
    return False


class TestChronoContractHandshake(unittest.TestCase):
    """Engine Integration Contract v1 against the real C++ daemon.

    Build it with::

        cd chrono_daemon && mkdir -p build && cd build && cmake .. && make
    """

    _PORT = 29881

    @classmethod
    def setUpClass(cls) -> None:
        cls._proc = subprocess.Popen(  # noqa: S603 — local test binary
            [str(_DAEMON_PATH), "--port", str(cls._PORT)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={**os.environ, "LD_LIBRARY_PATH": _CHRONO_LIB_DIR},
        )
        if not _wait_for_listening("127.0.0.1", cls._PORT):
            cls._proc.kill()
            raise unittest.SkipTest("chrono_daemon did not start listening")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._proc.terminate()
        try:
            cls._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls._proc.kill()

    def _call(self, payload: dict) -> dict:
        with socket.create_connection(("127.0.0.1", self._PORT), timeout=5.0) as sock:
            sock.sendall((json.dumps(payload) + "\n").encode())
            buf = b""
            while b"\n" not in buf:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
            return json.loads(buf.split(b"\n")[0].decode())

    def test_hello(self) -> None:
        resp = self._call({"cmd": "hello", "args": {}})
        self.assertTrue(resp["ok"], resp)
        result = resp["result"]
        self.assertEqual(result["protocol_version"], "1.0.0")
        self.assertEqual(result["contract_versions_supported"], ["1"])
        self.assertEqual(result["engine"], "chrono")
        self.assertEqual(result["runtime_mode"], "real")

        caps = result["capabilities"]
        self.assertEqual(caps["modes"], ["batch"])
        self.assertEqual(caps["formats"], ["chrono_spec", "mechanism"])
        self.assertEqual(caps["features"], [])
        self.assertEqual(caps["fields"], {"emits": [], "accepts": []})

    def test_request_id_round_trip(self) -> None:
        self.assertEqual(self._call({"cmd": "hello", "request_id": "rid-1"})["request_id"], "rid-1")
        self.assertNotIn("request_id", self._call({"cmd": "hello"}))

    def test_unknown_command(self) -> None:
        resp = self._call({"cmd": "definitely_not_a_verb", "request_id": "rid-2"})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["code"], "UNSUPPORTED_COMMAND")
        self.assertEqual(resp["request_id"], "rid-2")


if __name__ == "__main__":
    unittest.main()
