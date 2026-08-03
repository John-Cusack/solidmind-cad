"""How motion.* picks and drives an engine, now that nothing branches on names.

These replace the per-engine integration suites (Isaac's and Gazebo's), which
tested code paths that no longer exist separately.  What matters now is that
the *contract* decides: the registry supplies the vocabulary, the handshake
supplies the behaviour, and one adapter carries the traffic.
"""

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch

from server import motion_store
from server.tools_motion import (
    motion_define_mechanism,
    motion_screenshot,
    motion_simulate,
    motion_teleop_command,
    motion_teleop_start,
    motion_teleop_stop,
)


def _mechanism_id() -> str:
    motion_store.clear()
    result = motion_define_mechanism(
        {
            "name": "router",
            "parts": [{"id": "frame", "is_ground": True}, {"id": "link_a"}],
            "joints": [
                {
                    "id": "j1",
                    "joint_type": "revolute",
                    "parent_part": "frame",
                    "child_part": "link_a",
                }
            ],
            "drives": [{"joint_id": "j1", "speed_rpm": 60.0}],
        }
    )
    return str(result["mechanism_id"])


_SIMULATE_RESULT = {
    "ok": True,
    "time_series": [{"t": 0.0, "parts": {}}, {"t": 1.0, "parts": {}}],
    "summary": {"simulation_time_s": 1.0, "dt_s": 0.001, "engine_mode": "stub"},
}


class _Engine:
    """Stand-in for an engine: canned capabilities and canned responses."""

    def __init__(
        self,
        *,
        modes: list[str] | None = None,
        features: list[str] | None = None,
        teleop_dofs: list[str] | None = None,
        responses: dict[str, Any] | None = None,
    ) -> None:
        self.modes = modes if modes is not None else ["batch"]
        self.features = features or []
        self.dofs = teleop_dofs
        self.responses = responses or {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def call(self, engine: str, cmd: str, *, timeout: float | None = None, **args: Any):
        self.calls.append((engine, cmd, args))
        if cmd in self.responses:
            return self.responses[cmd]
        if cmd == "simulate":
            return dict(_SIMULATE_RESULT)
        if cmd == "teleop_start":
            return {"ok": True, "session_id": "sess_1", "status": "started"}
        return {"ok": True}

    def supports_mode(self, engine: str, mode: str) -> bool:
        return mode in self.modes

    def supports_feature(self, engine: str, feature: str) -> bool:
        return feature in self.features

    def teleop_dofs(self, engine: str) -> list[str] | None:
        return self.dofs

    def patches(self):
        return (
            patch("server.sim_adapter.call", side_effect=self.call),
            patch("server.sim_adapter.supports_mode", side_effect=self.supports_mode),
            patch("server.sim_adapter.supports_feature", side_effect=self.supports_feature),
            patch("server.sim_adapter.teleop_dofs", side_effect=self.teleop_dofs),
        )

    def __enter__(self) -> _Engine:
        self._active = [p.start() for p in self.patches()]
        return self

    def __exit__(self, *exc: object) -> None:
        patch.stopall()

    def commands(self) -> list[str]:
        return [cmd for _engine, cmd, _args in self.calls]


class TestBackendSelection(unittest.TestCase):
    def test_named_backend_is_used(self) -> None:
        mid = _mechanism_id()
        with _Engine() as engine:
            result = motion_simulate(mid, backend="chrono", duration_s=0.1)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["backend_used"], "chrono")
        self.assertEqual(engine.calls[0][0], "chrono")

    def test_default_backend_comes_from_the_registry(self) -> None:
        from server.engine_registry import default_engine

        mid = _mechanism_id()
        with _Engine() as engine:
            result = motion_simulate(mid, duration_s=0.1)
        self.assertEqual(result["backend_used"], default_engine())
        self.assertEqual(engine.calls[0][0], default_engine())

    def test_unknown_backend_lists_the_registered_ones(self) -> None:
        mid = _mechanism_id()
        result = motion_simulate(mid, backend="nonexistent")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "INVALID_INPUT")
        self.assertIn("chrono", result["error"]["message"])

    def test_teleop_is_refused_when_the_engine_does_not_advertise_it(self) -> None:
        mid = _mechanism_id()
        with _Engine(modes=["batch"]):
            result = motion_simulate(mid, backend="chrono", mode="teleop")
        self.assertFalse(result["ok"])
        self.assertIn("does not advertise teleop", result["error"]["message"])


class TestSimulateShape(unittest.TestCase):
    """Session vs single-call is a capability decision, not a name check."""

    def test_batch_engine_uses_one_simulate_call(self) -> None:
        mid = _mechanism_id()
        with _Engine(modes=["batch"]) as engine:
            result = motion_simulate(mid, backend="chrono", duration_s=0.1)
        self.assertTrue(result["ok"])
        self.assertEqual(engine.commands(), ["simulate"])

    def test_session_engine_starts_polls_and_stops(self) -> None:
        mid = _mechanism_id()
        responses = {
            "simulate_start": {"ok": True, "session_id": "s1", "steady_state_speeds": {"j1": 60}},
            "simulate_status": {"ok": True, "status": "complete"},
            "simulate_stop": {
                "ok": True,
                "samples": [{"t": 0.0}, {"t": 1.0}],
                "target_steps": 1000,
            },
        }
        with _Engine(modes=["batch", "session"], responses=responses) as engine:
            result = motion_simulate(mid, backend="isaac", duration_s=1.0)
        self.assertTrue(result["ok"], result)
        self.assertEqual(engine.commands(), ["simulate_start", "simulate_status", "simulate_stop"])
        self.assertEqual(len(result["time_series"]), 2)
        self.assertEqual(result["summary"]["steady_state_speeds"], {"j1": 60})

    def test_session_engine_falls_back_when_the_verb_is_missing(self) -> None:
        """An engine that advertises sessions but rejects the verb still runs."""
        responses = {
            "simulate_start": {
                "ok": False,
                "error": {"code": "UNSUPPORTED_COMMAND", "message": "no sessions here"},
            }
        }
        mid = _mechanism_id()
        with _Engine(modes=["batch", "session"], responses=responses) as engine:
            result = motion_simulate(mid, backend="isaac", duration_s=0.1)
        self.assertTrue(result["ok"], result)
        self.assertEqual(engine.commands(), ["simulate_start", "simulate"])

    def test_samples_are_normalized_to_time_series(self) -> None:
        responses = {"simulate": {"ok": True, "samples": [{"t": 0.0}, {"t": 2.0}]}}
        mid = _mechanism_id()
        with _Engine(responses=responses):
            result = motion_simulate(mid, backend="gazebo", duration_s=2.0)
        self.assertEqual(len(result["time_series"]), 2)
        self.assertAlmostEqual(result["summary"]["simulation_time_s"], 2.0)

    def test_unavailable_engine_returns_the_choice_contract(self) -> None:
        responses = {
            "simulate": {
                "ok": False,
                "error": {"code": "ENGINE_NOT_CONNECTED", "message": "not running"},
            }
        }
        mid = _mechanism_id()
        with _Engine(responses=responses):
            result = motion_simulate(mid, backend="gazebo", duration_s=0.1)
        self.assertFalse(result["ok"])
        # The tool answers with a choice contract rather than a bare failure.
        self.assertEqual(result["error"]["code"], "BACKEND_UNAVAILABLE_CHOOSE")
        self.assertIn("not running", result["error"]["message"])
        actions = {c["action"] for c in result["choices"]}
        self.assertIn("retry_with_backend", actions)
        # Alternatives are the other registered engines — from the registry.
        offered = {c.get("backend") for c in result["choices"]}
        self.assertIn("chrono", offered)
        self.assertIn("isaac", offered)


class TestTeleopRouting(unittest.TestCase):
    def test_lifecycle_goes_to_one_engine(self) -> None:
        mid = _mechanism_id()
        with _Engine(modes=["batch", "teleop"]) as engine:
            started = motion_teleop_start(mid, backend="gazebo")
            self.assertTrue(started["ok"], started)
            session_id = started["session_id"]

            commanded = motion_teleop_command(session_id, vx_mps=0.5)
            self.assertTrue(commanded["ok"], commanded)

            stopped = motion_teleop_stop(session_id)
            self.assertTrue(stopped["ok"], stopped)

        self.assertEqual(engine.commands(), ["teleop_start", "teleop_command", "teleop_stop"])
        self.assertTrue(all(call[0] == "gazebo" for call in engine.calls))

    def test_command_on_an_unsupported_axis_is_refused(self) -> None:
        """Declared teleop axes beat silently-dropped setpoints."""
        mid = _mechanism_id()
        dofs = ["vx_mps", "yaw_rate_rps", "body_height_m"]
        with _Engine(modes=["teleop"], teleop_dofs=dofs):
            session_id = motion_teleop_start(mid, backend="isaac")["session_id"]
            result = motion_teleop_command(session_id, vy_mps=0.4)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "INVALID_INPUT")
        self.assertIn("vy_mps", result["error"]["message"])

    def test_supported_axes_are_forwarded(self) -> None:
        mid = _mechanism_id()
        dofs = ["vx_mps", "vy_mps", "vz_mps", "yaw_rate_rps", "body_height_m"]
        with _Engine(modes=["teleop"], teleop_dofs=dofs) as engine:
            session_id = motion_teleop_start(mid, backend="gazebo")["session_id"]
            result = motion_teleop_command(session_id, vy_mps=0.4, vz_mps=-0.2)
        self.assertTrue(result["ok"], result)
        command_args = [args for _e, cmd, args in engine.calls if cmd == "teleop_command"][0]
        self.assertAlmostEqual(command_args["vy_mps"], 0.4)
        self.assertAlmostEqual(command_args["vz_mps"], -0.2)


class TestScreenshotGating(unittest.TestCase):
    def test_engine_without_the_feature_says_so(self) -> None:
        with _Engine(features=[]):
            result = motion_screenshot(backend="chrono")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "UNSUPPORTED_CAPABILITY")

    def test_engine_with_the_feature_is_called(self) -> None:
        responses = {"screenshot": {"ok": True, "image_base64": "iVBOR"}}
        with _Engine(features=["screenshot"], responses=responses) as engine:
            result = motion_screenshot(backend="isaac", width=640)
        self.assertTrue(result["ok"], result)
        self.assertEqual(engine.commands(), ["screenshot"])
        self.assertEqual(engine.calls[0][2]["width"], 640)


if __name__ == "__main__":
    unittest.main()
