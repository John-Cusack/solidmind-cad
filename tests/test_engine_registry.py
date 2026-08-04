"""Tests for the engine registry and the generic client.

Together these are what makes an engine addable without a core edit: the
registry turns descriptor files into core's backend vocabulary, and the client
speaks the one contract every engine implements.
"""

from __future__ import annotations

import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from server import engine_registry as registry
from server.engine_client import (
    EngineClient,
    EngineCommandError,
    EngineConnectionError,
    tripwire,
)


class _DescriptorDir:
    """A temporary ``engines.d`` the registry reads instead of the user's."""

    def __init__(self, files: dict[str, str]) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        for name, body in files.items():
            (Path(self._tmp.name) / name).write_text(body)
        self._patch = patch.dict("os.environ", {"SOLIDMIND_ENGINES_D": self._tmp.name})

    def __enter__(self) -> _DescriptorDir:
        self._patch.start()
        registry.reset_cache()
        return self

    def __exit__(self, *exc: object) -> None:
        self._patch.stop()
        registry.reset_cache()
        self._tmp.cleanup()


class TestBuiltinDescriptors(unittest.TestCase):
    def setUp(self) -> None:
        registry.reset_cache()
        self.addCleanup(registry.reset_cache)

    def test_core_engines_are_registered(self) -> None:
        # The reference engine ships with core and is always present; the other
        # three are core's defaults for engines that live in sibling repos.
        self.assertEqual(registry.engine_names(), ["chrono", "gazebo", "isaac", "reference"])

    def test_every_engine_carries_guidance_and_a_hint(self) -> None:
        """Descriptors are what make an engine recommendable (Principle 8)."""
        for name in registry.engine_names():
            self.assertTrue(registry.when_to_use(name), f"{name} has no when_to_use")
            self.assertTrue(registry.install_hint(name), f"{name} has no install_hint")

    def test_ports_match_the_documented_defaults(self) -> None:
        self.assertEqual(registry.resolve_port("chrono"), 9877)
        self.assertEqual(registry.resolve_port("isaac"), 9878)
        self.assertEqual(registry.resolve_port("gazebo"), 9879)

    def test_env_overrides_the_port(self) -> None:
        with patch.dict("os.environ", {"SOLIDMIND_GAZEBO_PORT": "19999"}):
            self.assertEqual(registry.resolve_port("gazebo"), 19999)

    def test_launch_command_substitutes_the_resolved_port(self) -> None:
        argv = registry.launch_argv("gazebo", port=12345)
        self.assertIn("12345", argv)
        self.assertNotIn("${PORT}", " ".join(argv))

    def test_variants_select_an_alternate_launch(self) -> None:
        default = registry.launch_argv("gazebo", port=9879)
        real = registry.launch_argv("gazebo", port=9879, variant="real")
        self.assertIn("stub", default)
        self.assertNotEqual(default, real)
        self.assertIn("run_gazebo_bridge.sh", " ".join(real))

    def test_unknown_variant_falls_back_to_the_default(self) -> None:
        self.assertEqual(
            registry.launch_argv("gazebo", port=9879, variant="nonexistent"),
            registry.launch_argv("gazebo", port=9879),
        )


class TestThirdPartyDescriptors(unittest.TestCase):
    """The N+1 rule: a new engine is a file, not a core edit."""

    def test_a_dropped_in_descriptor_joins_the_vocabulary(self) -> None:
        with _DescriptorDir(
            {
                "mujoco.toml": (
                    'name = "mujoco"\n'
                    "port = 9899\n"
                    'when_to_use = "Contact-rich manipulation."\n'
                    'launch = ["mjbridge", "--port", "${PORT}"]\n'
                )
            }
        ):
            self.assertIn("mujoco", registry.engine_names())
            self.assertEqual(registry.resolve_port("mujoco"), 9899)
            self.assertEqual(
                registry.launch_argv("mujoco", port=9899), ["mjbridge", "--port", "9899"]
            )
            self.assertEqual(registry.when_to_use("mujoco"), "Contact-rich manipulation.")

    def test_a_user_descriptor_overrides_a_core_one(self) -> None:
        with _DescriptorDir({"gazebo.toml": 'name = "gazebo"\nport = 18000\n'}):
            self.assertEqual(registry.resolve_port("gazebo"), 18000)

    def test_attach_only_descriptor_has_no_launch(self) -> None:
        """No launch command = a user-run or remote engine core only dials."""
        with _DescriptorDir({"remote.toml": 'name = "remote"\nport = 9999\nhost = "10.0.0.5"\n'}):
            descriptor = registry.get_descriptor("remote")
            self.assertTrue(descriptor.attach_only)
            self.assertIsNone(registry.launch_argv("remote", port=9999))
            self.assertEqual(registry.resolve_host("remote"), "10.0.0.5")

    def test_a_broken_descriptor_is_skipped_not_fatal(self) -> None:
        """One bad third-party file must not take the tool surface down."""
        with _DescriptorDir(
            {
                "broken.toml": "this is not = valid = toml",
                "good.toml": 'name = "good"\nport = 9998\n',
            }
        ):
            names = registry.engine_names()
            self.assertIn("good", names)
            self.assertNotIn("broken", names)

    def test_descriptor_without_a_port_is_rejected(self) -> None:
        with _DescriptorDir({"noport.toml": 'name = "noport"\n'}):
            self.assertNotIn("noport", registry.engine_names())


class TestSubstitution(unittest.TestCase):
    def test_defaults_and_environment(self) -> None:
        argv = ("${A}", "${MISSING:-fallback}", "plain")
        self.assertEqual(registry.substitute(argv, {"A": "value"}), ["value", "fallback", "plain"])

    def test_values_win_over_the_environment(self) -> None:
        with patch.dict("os.environ", {"PORT": "1"}):
            self.assertEqual(registry.substitute(("${PORT}",), {"PORT": "2"}), ["2"])


class _StubEngine:
    """Minimal contract-speaking server for client tests."""

    def __init__(self, *, capabilities: dict[str, Any] | None = None) -> None:
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.settimeout(0.2)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(4)
        self.port = int(self._srv.getsockname()[1])
        self.capabilities = capabilities or {"modes": ["batch"], "features": []}
        self.hello_calls = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> _StubEngine:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=3.0)
        try:
            self._srv.close()
        except OSError:
            pass

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except (TimeoutError, OSError):
                continue
            with conn:
                conn.settimeout(0.5)
                buf = b""
                while not self._stop.is_set():
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
                        conn.sendall((json.dumps(self._reply(json.loads(line))) + "\n").encode())

    def _reply(self, msg: dict[str, Any]) -> dict[str, Any]:
        cmd = msg.get("cmd")
        response: dict[str, Any]
        if cmd == "hello":
            self.hello_calls += 1
            response = {
                "ok": True,
                "result": {
                    "protocol_version": "1.0.0",
                    "engine": "stub",
                    "capabilities": self.capabilities,
                },
            }
        elif cmd == "ping":
            response = {"ok": True, "result": {"pong": True}}
        elif cmd == "boom":
            response = {"ok": False, "error": {"code": "ENGINE_ERROR", "message": "kaboom"}}
        else:
            response = {"ok": True, "result": {"echo": msg.get("args", {})}}
        if isinstance(msg.get("request_id"), str):
            response["request_id"] = msg["request_id"]
        return response


class TestEngineClient(unittest.TestCase):
    def test_hello_is_cached_per_connection(self) -> None:
        with _StubEngine() as engine:
            client = EngineClient("stub", host="127.0.0.1", port=engine.port)
            client.connect()
            try:
                client.hello()
                client.hello()
                client.capabilities()
                self.assertEqual(engine.hello_calls, 1)
                client.hello(refresh=True)
                self.assertEqual(engine.hello_calls, 2)
            finally:
                client.disconnect()

    def test_capability_queries(self) -> None:
        caps = {"modes": ["batch", "teleop"], "features": ["screenshot"], "formats": ["package"]}
        with _StubEngine(capabilities=caps) as engine:
            client = EngineClient("stub", host="127.0.0.1", port=engine.port)
            client.connect()
            try:
                self.assertTrue(client.supports_mode("teleop"))
                self.assertFalse(client.supports_mode("session"))
                self.assertTrue(client.supports_feature("screenshot"))
                self.assertTrue(client.supports_format("package"))
            finally:
                client.disconnect()

    def test_error_carries_the_engine_code(self) -> None:
        with _StubEngine() as engine:
            client = EngineClient("stub", host="127.0.0.1", port=engine.port)
            client.connect()
            try:
                with self.assertRaises(EngineCommandError) as ctx:
                    client.send_command("boom")
                self.assertEqual(ctx.exception.code, "ENGINE_ERROR")
            finally:
                client.disconnect()

    def test_request_id_round_trips(self) -> None:
        with _StubEngine() as engine:
            client = EngineClient("stub", host="127.0.0.1", port=engine.port)
            client.connect()
            try:
                self.assertEqual(client.send_command("echo", request_id="r1"), {"echo": {}})
            finally:
                client.disconnect()

    def test_connection_refused(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            dead_port = int(probe.getsockname()[1])
        client = EngineClient("stub", host="127.0.0.1", port=dead_port)
        with self.assertRaises(EngineConnectionError) as ctx:
            client.connect(timeout=0.5)
        # The message tells the model how to fix it.
        self.assertIn("sim.start_engine", str(ctx.exception))


class TestRateTripwire(unittest.TestCase):
    """Core must never end up inside a physics-rate loop (Principle 5)."""

    def setUp(self) -> None:
        tripwire.reset()
        self.addCleanup(tripwire.reset)

    def test_normal_traffic_is_silent(self) -> None:
        with self.assertNoLogs("solidmind.engine_client", level="WARNING"):
            for _ in range(10):
                tripwire.record("stub", "simulate")

    def test_sustained_rate_warns_once(self) -> None:
        with self.assertLogs("solidmind.engine_client", level="WARNING") as logs:
            for _ in range(300):
                tripwire.record("stub", "teleop_command")
        self.assertEqual(len(logs.records), 1, "should warn once, not per message")
        self.assertIn("teleop_command", logs.output[0])
        self.assertIn("msg/s", logs.output[0])

    def test_rate_is_per_command(self) -> None:
        for _ in range(300):
            tripwire.record("stub", "teleop_command")
        self.assertGreater(tripwire.rate("stub", "teleop_command"), 100.0)
        self.assertEqual(tripwire.rate("stub", "simulate"), 0.0)

    def test_old_events_leave_the_window(self) -> None:
        tripwire.record("stub", "ping")
        self.assertGreater(tripwire.rate("stub", "ping"), 0.0)
        with patch("time.monotonic", return_value=time.monotonic() + 5.0):
            self.assertEqual(tripwire.rate("stub", "ping"), 0.0)


class TestShippedDescriptorsPointAtRealCommands(unittest.TestCase):
    """A descriptor is only as good as the command it names.

    The split moved every engine into its own repository, and two descriptors
    kept launching ``scripts/run_<engine>_bridge.sh`` — wrappers that stayed
    behind in core's history and were never copied across. Core would have
    tried to start a script that did not exist, and nothing failed until
    someone ran ``sim.start_engine``.

    Only checkable where the engine is actually cloned, so each case skips
    when its ``cwd`` is absent. That is enough: it fires on the machines that
    have the engine, which are the machines that can launch it.
    """

    def _descriptors(self) -> list[tuple[str, Path, tuple[str, ...]]]:
        from server.engine_registry import engine_names, get_descriptor, reset_cache

        reset_cache()
        out = []
        for name in engine_names():
            descriptor = get_descriptor(name)
            if not descriptor.cwd:
                continue
            root = Path(descriptor.cwd).expanduser()
            for variant in (None, *sorted(getattr(descriptor, "variants", {}) or {})):
                out.append((name, root, descriptor.launch_command(variant)))
        return out

    def test_every_launched_script_exists_where_the_engine_is_cloned(self) -> None:
        checked = 0
        for name, root, argv in self._descriptors():
            if not root.is_dir():
                continue
            for token in argv:
                # Relative paths ending in a runnable script are the ones that
                # can silently go missing; flags and module names cannot.
                if token.startswith("-") or "/" not in token or token.startswith("/"):
                    continue
                target = root / token
                self.assertTrue(
                    target.is_file(),
                    f"{name}: descriptor launches {token!r}, missing at {target}",
                )
                checked += 1
        if checked == 0:
            self.skipTest("no engine repositories are cloned on this machine")


if __name__ == "__main__":
    unittest.main()
