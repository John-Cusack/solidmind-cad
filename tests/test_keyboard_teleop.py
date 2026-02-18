"""Tests for Isaac keyboard teleop key mapping."""
from __future__ import annotations

import unittest

from isaac_bridge.keyboard_teleop import KeyboardTeleopMapper


class TestKeyboardTeleopMapper(unittest.TestCase):
    def test_bindings(self) -> None:
        mapper = KeyboardTeleopMapper(linear_speed_mps=0.5, yaw_speed_rps=1.0, height_step_m=0.02)
        cmd = mapper.from_pressed_keys({"W", "A", "Q"}, current_height_m=0.1)
        self.assertAlmostEqual(cmd.vx_mps, 0.5)
        self.assertAlmostEqual(cmd.yaw_rate_rps, 1.0)
        self.assertAlmostEqual(cmd.body_height_m, 0.12)

    def test_canceling_keys(self) -> None:
        mapper = KeyboardTeleopMapper()
        cmd = mapper.from_pressed_keys({"W", "S", "A", "D"}, current_height_m=0.0)
        self.assertAlmostEqual(cmd.vx_mps, 0.0)
        self.assertAlmostEqual(cmd.yaw_rate_rps, 0.0)


if __name__ == "__main__":
    unittest.main()
