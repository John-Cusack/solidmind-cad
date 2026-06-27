"""Tests for the controller registry pattern (P7).

Verifies that:
- The registry resolves known controller types.
- Unknown types raise ValueError with available list.
- HexapodTripodController behavior is identical before/after extraction.
- Adding a custom controller works via the registry API.
"""

from __future__ import annotations

import math
import unittest

from isaac_bridge.controllers import (
    _CONTROLLER_REGISTRY,
    Hexapod3DOFController,
    HexapodTripodController,
    PolicyController,
    create_controller,
)
from isaac_bridge.models import Controller, TeleopConfig, TeleopState

_DEG2RAD = math.pi / 180.0


class TestRegistryLookup(unittest.TestCase):
    """create_controller resolves known types and rejects unknown ones."""

    def test_hexapod_default(self) -> None:
        config = TeleopConfig()
        ctrl = create_controller(config)
        self.assertIsInstance(ctrl, HexapodTripodController)
        self.assertIsInstance(ctrl, Controller)

    def test_hexapod_3dof(self) -> None:
        config = TeleopConfig.from_profile(
            {
                "controller_type": "hexapod_3dof_tripod",
            }
        )
        ctrl = create_controller(config)
        self.assertIsInstance(ctrl, Hexapod3DOFController)
        self.assertIsInstance(ctrl, Controller)

    def test_rl_residual(self) -> None:
        config = TeleopConfig.from_profile(
            {
                "controller_type": "rl_residual",
                "policy_path": "/nonexistent/policy.pt",
            }
        )
        ctrl = create_controller(config)
        self.assertIsInstance(ctrl, PolicyController)
        self.assertIsInstance(ctrl, Controller)

    def test_unknown_type_raises(self) -> None:
        config = TeleopConfig.from_profile(
            {
                "controller_type": "rl_residual",  # need valid parse first
            }
        )
        # Manually override to an unknown type for the test
        object.__setattr__(config, "controller_type", "nonexistent_type")
        with self.assertRaises(ValueError) as ctx:
            create_controller(config)
        self.assertIn("nonexistent_type", str(ctx.exception))
        self.assertIn("hexapod_1dof_tripod", str(ctx.exception))

    def test_registry_has_expected_entries(self) -> None:
        self.assertIn("hexapod_1dof_tripod", _CONTROLLER_REGISTRY)
        self.assertIn("hexapod_3dof_tripod", _CONTROLLER_REGISTRY)
        self.assertIn("rl_residual", _CONTROLLER_REGISTRY)


class TestRegistryExtensibility(unittest.TestCase):
    """Verify a custom controller can be registered and used."""

    def setUp(self) -> None:
        # Register a trivial custom controller for testing
        self._original_registry = dict(_CONTROLLER_REGISTRY)

    def tearDown(self) -> None:
        # Restore original registry
        _CONTROLLER_REGISTRY.clear()
        _CONTROLLER_REGISTRY.update(self._original_registry)

    def test_add_custom_controller(self) -> None:
        class StubController:
            """Minimal Controller-protocol-compliant stub."""

            @property
            def filtered_vx(self) -> float:
                return 0.0

            @property
            def filtered_yaw(self) -> float:
                return 0.0

            @property
            def filtered_height(self) -> float:
                return 0.0

            def compute_targets(
                self,
                state: TeleopState,
                dt_s: float,
                config: TeleopConfig,
                phase: float,
            ) -> tuple[dict[str, float], float]:
                return {name: 0.0 for name in config.joint_names}, phase

        _CONTROLLER_REGISTRY["stub_test"] = lambda cfg: StubController()

        config = TeleopConfig.from_profile(
            {
                "controller_type": "rl_residual",  # parse valid, then override
            }
        )
        object.__setattr__(config, "controller_type", "stub_test")
        ctrl = create_controller(config)
        self.assertIsInstance(ctrl, Controller)

        targets, phase = ctrl.compute_targets(
            TeleopState(),
            0.01,
            TeleopConfig(),
            0.0,
        )
        self.assertEqual(phase, 0.0)
        self.assertTrue(all(v == 0.0 for v in targets.values()))


class TestHexapodRegressionAfterExtraction(unittest.TestCase):
    """Verify HexapodTripodController output is unchanged after registry extraction.

    These are golden-value regression tests: the controller math must
    produce the same outputs regardless of how the controller is
    instantiated (direct vs. registry).
    """

    def _compute_via_registry(
        self,
        state: TeleopState,
        dt_s: float,
        config: TeleopConfig,
        phase: float,
    ) -> tuple[dict[str, float], float]:
        ctrl = create_controller(config)
        return ctrl.compute_targets(state, dt_s, config, phase)

    def _compute_directly(
        self,
        state: TeleopState,
        dt_s: float,
        config: TeleopConfig,
        phase: float,
    ) -> tuple[dict[str, float], float]:
        ctrl = HexapodTripodController()
        return ctrl.compute_targets(state, dt_s, config, phase)

    def test_zero_command_identical(self) -> None:
        state = TeleopState()
        config = TeleopConfig()
        t_reg, p_reg = self._compute_via_registry(state, 0.01, config, 0.0)
        t_dir, p_dir = self._compute_directly(state, 0.01, config, 0.0)
        self.assertAlmostEqual(p_reg, p_dir, places=10)
        for name in config.joint_names:
            self.assertAlmostEqual(t_reg[name], t_dir[name], places=10, msg=f"Mismatch for {name}")

    def test_forward_command_identical(self) -> None:
        state = TeleopState(vx_mps=0.3, yaw_rate_rps=0.0, body_height_m=0.0)
        config = TeleopConfig()
        t_reg, p_reg = self._compute_via_registry(state, 0.016, config, 1.5)
        t_dir, p_dir = self._compute_directly(state, 0.016, config, 1.5)
        self.assertAlmostEqual(p_reg, p_dir, places=10)
        for name in config.joint_names:
            self.assertAlmostEqual(t_reg[name], t_dir[name], places=10)

    def test_yaw_command_identical(self) -> None:
        state = TeleopState(vx_mps=0.0, yaw_rate_rps=0.8, body_height_m=0.0)
        config = TeleopConfig()
        t_reg, p_reg = self._compute_via_registry(state, 0.01, config, 3.0)
        t_dir, p_dir = self._compute_directly(state, 0.01, config, 3.0)
        self.assertAlmostEqual(p_reg, p_dir, places=10)
        for name in config.joint_names:
            self.assertAlmostEqual(t_reg[name], t_dir[name], places=10)

    def test_mixed_command_identical(self) -> None:
        state = TeleopState(vx_mps=0.4, yaw_rate_rps=-0.5, body_height_m=0.02)
        config = TeleopConfig()
        t_reg, p_reg = self._compute_via_registry(state, 0.02, config, 5.0)
        t_dir, p_dir = self._compute_directly(state, 0.02, config, 5.0)
        self.assertAlmostEqual(p_reg, p_dir, places=10)
        for name in config.joint_names:
            self.assertAlmostEqual(t_reg[name], t_dir[name], places=10)

    def test_custom_config_identical(self) -> None:
        state = TeleopState(vx_mps=0.2, yaw_rate_rps=0.3, body_height_m=0.01)
        config = TeleopConfig.from_profile(
            {
                "amplitude_deg": 25.0,
                "stride_hz": 2.0,
                "yaw_mix_deg": 12.0,
            }
        )
        t_reg, p_reg = self._compute_via_registry(state, 0.01, config, 0.5)
        t_dir, p_dir = self._compute_directly(state, 0.01, config, 0.5)
        self.assertAlmostEqual(p_reg, p_dir, places=10)
        for name in config.joint_names:
            self.assertAlmostEqual(t_reg[name], t_dir[name], places=10)


class TestRuntimeRegistryIntegration(unittest.TestCase):
    """Verify the runtime uses the registry for controller creation."""

    def test_teleop_start_uses_registry(self) -> None:
        from isaac_bridge.runtime_isaac import IsaacRuntime

        runtime = IsaacRuntime(headless=True)
        result = runtime.teleop_start(
            mechanism={
                "name": "test",
                "parts": [{"id": "body", "is_ground": False}],
                "joints": [],
                "drives": [],
            }
        )
        self.assertIn("session_id", result)
        self.assertEqual(result["controller_type"], "hexapod_1dof_tripod")

    def test_unknown_controller_type_returns_error(self) -> None:
        from isaac_bridge.runtime_isaac import IsaacRuntime, IsaacRuntimeError

        runtime = IsaacRuntime(headless=True)
        with self.assertRaises(IsaacRuntimeError) as ctx:
            runtime.teleop_start(
                mechanism={
                    "name": "test",
                    "parts": [{"id": "body", "is_ground": False}],
                    "joints": [],
                    "drives": [],
                },
                profile={"controller_type": "nonexistent"},
            )
        self.assertEqual(ctx.exception.code, "INVALID_INPUT")
        self.assertIn("nonexistent", ctx.exception.message)

    def test_bridge_unknown_controller_returns_error(self) -> None:
        import json

        from isaac_bridge.bridge_server import BridgeServer

        server = BridgeServer(host="127.0.0.1", port=0, headless=True)
        result = server._handle_line(
            json.dumps(
                {
                    "cmd": "teleop_start",
                    "args": {
                        "mechanism": {
                            "name": "test",
                            "parts": [{"id": "body", "is_ground": False}],
                            "joints": [],
                            "drives": [],
                        },
                        "profile": {"controller_type": "nonexistent"},
                    },
                }
            ).encode("utf-8")
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "INVALID_INPUT")


if __name__ == "__main__":
    unittest.main()
