"""The TCK, run against the reference engine — core's conformance gate.

This is what lets core's CI claim contract conformance with zero engines
installed, and it is the check that keeps the TCK itself honest: a failure
here means either the reference engine or the kit is wrong, and both are ours.

Replaces ``tests/test_sim_cross_backend.py``, whose response-shape assertions
are now tier 3.
"""

from __future__ import annotations

import socket
import threading
import time
import unittest
from pathlib import Path

from reference_engine.bridge_server import ReferenceBridgeServer
from tck.client import TckClient
from tck.report import Outcome, TierResult
from tck.runner import run_tck

_FIXTURE_PACKAGE = Path(__file__).resolve().parents[1] / "tck" / "fixtures" / "golden_package"


def _unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _ReferenceEngine:
    """Run the reference engine in a thread for the duration of a test."""

    def __init__(self) -> None:
        self.port = _unused_port()
        self._server = ReferenceBridgeServer(port=self.port)
        self._thread: threading.Thread | None = None

    def __enter__(self) -> _ReferenceEngine:
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    return self
            except OSError:
                time.sleep(0.02)
        raise RuntimeError("reference engine did not start")

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        if self._thread is not None:
            self._thread.join(timeout=5.0)


class TestReferenceEngineIsConformant(unittest.TestCase):
    """Every tier, against the engine core ships."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._engine = _ReferenceEngine().__enter__()
        cls.report = run_tck(
            port=cls._engine.port,
            package_dir=_FIXTURE_PACKAGE,
            latency_samples=20,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._engine.__exit__()

    def test_overall_conformance(self) -> None:
        self.assertTrue(
            self.report.passed,
            "reference engine is not conformant:\n" + self.report.render(),
        )

    def test_identifies_itself(self) -> None:
        self.assertEqual(self.report.engine, "reference")
        self.assertTrue(self.report.engine_version)

    def test_every_tier_ran(self) -> None:
        tiers = [t.tier for t in self.report.tiers]
        self.assertEqual(len(tiers), 6, tiers)

    def test_no_tier_was_vacuous(self) -> None:
        """A tier with no checks would pass by doing nothing."""
        for tier in self.report.tiers:
            self.assertTrue(tier.checks, f"{tier.tier} recorded no checks")

    def test_physics_tier_actually_checked_physics(self) -> None:
        """The reference engine simulates, so nothing here may be skipped."""
        physics = self._tier("5. physics sanity")
        skipped = [c.name for c in physics.checks if c.outcome is Outcome.SKIP]
        self.assertEqual(skipped, [], f"physics checks skipped: {skipped}")
        self.assertGreaterEqual(len(physics.checks), 3)

    def test_sessions_and_teleop_were_exercised(self) -> None:
        sessions = self._tier("4. sessions/teleop")
        skipped = [c.name for c in sessions.checks if c.outcome is Outcome.SKIP]
        self.assertEqual(skipped, [], f"session checks skipped: {skipped}")

    def test_package_tier_ingested_the_golden_fixture(self) -> None:
        package = self._tier("2. package")
        self.assertTrue(any(c.outcome is Outcome.PASS for c in package.checks))

    def test_report_renders_and_serializes(self) -> None:
        rendered = self.report.render()
        self.assertIn("RESULT: conformant", rendered)
        self.assertIn("1. protocol", rendered)
        self.assertTrue(self.report.to_json().startswith("{"))

    def _tier(self, name: str) -> TierResult:
        return next(t for t in self.report.tiers if t.tier == name)


class TestTckCatchesNonConformance(unittest.TestCase):
    """The kit has to fail things — a TCK that always passes is worthless."""

    class _BadEngine(_ReferenceEngine):
        """A reference engine that lies about one capability."""

        def __enter__(self) -> _ReferenceEngine:
            engine = super().__enter__()
            # Claim screenshots without implementing them.
            self._server._runtime.hello()  # warm any state
            from reference_engine import runtime as runtime_module

            runtime_module.CAPABILITIES["features"].append("screenshot")
            return engine

        def __exit__(self, *exc: object) -> None:
            from reference_engine import runtime as runtime_module

            if "screenshot" in runtime_module.CAPABILITIES["features"]:
                runtime_module.CAPABILITIES["features"].remove("screenshot")
            super().__exit__(*exc)

    def test_capability_dishonesty_is_caught(self) -> None:
        with self._BadEngine() as engine:
            report = run_tck(port=engine.port, tiers=("protocol",), latency_samples=1)
        self.assertFalse(report.passed)
        failures = [name for _tier, check in report.failures for name in [check.name]]
        self.assertIn("advertised verb 'screenshot' is implemented", failures)

    def test_physics_is_skipped_when_mechanism_is_not_advertised(self) -> None:
        """An engine is judged on what it claims, not on what the kit prefers.

        The physics scenarios are in-band ``mechanism`` dicts.  Gazebo ingests
        packages, SDF and URDF — never a mechanism — so running them against it
        measured nothing and failed it for the result.  A capability the engine
        never advertised is the kit's problem to skip, exactly as tiers 2 and 4
        already do for ``package`` and ``session``.
        """
        from reference_engine import runtime as runtime_module

        formats = runtime_module.CAPABILITIES["formats"]
        removed = "mechanism" in formats
        if removed:
            formats.remove("mechanism")
        try:
            with _ReferenceEngine() as engine:
                report = run_tck(port=engine.port, tiers=("physics",), latency_samples=1)
        finally:
            if removed:
                formats.append("mechanism")

        self.assertTrue(report.passed)
        physics = next(t for t in report.tiers if t.tier == "5. physics sanity")
        self.assertEqual([c.outcome for c in physics.checks], [Outcome.SKIP])
        self.assertIn("'mechanism' not advertised", physics.checks[0].detail or "")

    def test_unavailable_engine_passes_when_it_refuses_to_simulate(self) -> None:
        """An engine whose backend is missing is honest, not broken.

        ``unavailable`` exists because that state was unsayable: a shim with no
        backend is not driving an engine, so not ``real``, and has nothing
        in-memory either, so not ``stub``. Reporting it and refusing to
        simulate is correct behaviour and the kit says so.
        """
        from reference_engine import runtime as runtime_module

        original = runtime_module.RUNTIME_MODE
        runtime_module.RUNTIME_MODE = "unavailable"
        try:
            with _ReferenceEngine() as engine:
                report = run_tck(port=engine.port, tiers=("results", "physics"), latency_samples=1)
        finally:
            runtime_module.RUNTIME_MODE = original

        results = next(t for t in report.tiers if t.tier == "3. results")
        outcomes = [c.outcome for c in results.checks]
        self.assertIn(Outcome.SKIP, outcomes, [c.name for c in results.checks])
        physics = next(t for t in report.tiers if t.tier == "5. physics sanity")
        self.assertEqual([c.outcome for c in physics.checks], [Outcome.SKIP])
        self.assertIn("unavailable", physics.checks[0].detail or "")

    def test_unavailable_engine_that_still_answers_is_caught(self) -> None:
        """Claiming unavailable and then returning numbers is fabrication.

        This is the failure the value exists to expose, so the kit must fail it
        rather than take the label at face value.
        """
        from reference_engine import runtime as runtime_module

        original = runtime_module.RUNTIME_MODE
        runtime_module.RUNTIME_MODE = "unavailable"
        try:
            with _ReferenceEngine() as engine:
                report = run_tck(port=engine.port, tiers=("results",), latency_samples=1)
        finally:
            runtime_module.RUNTIME_MODE = original

        # The reference engine simulates happily, so it contradicts its own
        # claim — exactly the case this check is for.
        self.assertFalse(report.passed)
        failures = [check.name for _tier, check in report.failures]
        self.assertIn("an unavailable engine refuses to simulate", failures)

    def test_unreachable_engine_raises(self) -> None:
        from tck.client import TckConnectionError

        with self.assertRaises(TckConnectionError):
            run_tck(port=_unused_port(), tiers=("protocol",))


class TestReferencePhysics(unittest.TestCase):
    """The analytic answers the physics tier checks against."""

    def test_gear_ratio(self) -> None:
        from reference_engine.physics import gear_train_speeds

        speeds = gear_train_speeds(
            {
                "parts": [{"id": "frame", "is_ground": True}, {"id": "a"}, {"id": "b"}],
                "joints": [
                    {
                        "id": "rev",
                        "joint_type": "revolute",
                        "parent_part": "frame",
                        "child_part": "a",
                    },
                    {
                        "id": "mesh",
                        "joint_type": "gear_mesh",
                        "parent_part": "a",
                        "child_part": "b",
                        "teeth_parent": 20,
                        "teeth_child": 40,
                    },
                ],
                "drives": [{"joint_id": "rev", "speed_rpm": 600.0}],
            }
        )
        self.assertAlmostEqual(speeds["a"], 600.0)
        # Half speed, and meshed gears turn opposite ways.
        self.assertAlmostEqual(speeds["b"], -300.0)
        self.assertAlmostEqual(speeds["frame"], 0.0)

    def test_pendulum_period_matches_theory(self) -> None:
        from reference_engine.physics import pendulum_angle, pendulum_period

        period = pendulum_period(1.0)
        self.assertAlmostEqual(period, 2.0060, places=3)
        self.assertAlmostEqual(pendulum_angle(1.0, 0.1, period), 0.1, places=6)
        self.assertAlmostEqual(pendulum_angle(1.0, 0.1, period / 2), -0.1, places=6)

    def test_free_fall(self) -> None:
        from reference_engine.physics import fall_height, settle_time

        self.assertAlmostEqual(settle_time(1.0), 0.4515, places=3)
        self.assertAlmostEqual(fall_height(1.0, 0.0), 1.0)
        self.assertEqual(fall_height(1.0, 10.0), 0.0)  # clamped at the ground


class TestReferenceEngineDirect(unittest.TestCase):
    """A few things the TCK cannot see from outside."""

    def test_shutdown_drains_sessions(self) -> None:
        with _ReferenceEngine() as engine, TckClient("127.0.0.1", engine.port) as client:
            client.result(
                "teleop_start",
                {"mechanism": {"name": "m", "parts": [{"id": "a"}], "joints": [], "drives": []}},
            )
            result = client.result("shutdown")
            self.assertEqual(result["sessions_drained"], 1)

    def test_package_without_links_is_rejected(self) -> None:
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "manifest.json").write_text(json.dumps({"name": "empty", "links": []}))
            with _ReferenceEngine() as engine, TckClient("127.0.0.1", engine.port) as client:
                response = client.request("simulate", {"package_path": tmp, "duration_s": 0.1})
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "PACKAGE_INVALID")


if __name__ == "__main__":
    unittest.main()
