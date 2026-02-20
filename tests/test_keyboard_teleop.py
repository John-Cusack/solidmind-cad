"""Tests for the keyboard teleop client key mapping (P4).

Tests the pure-function key mapping logic without requiring a terminal
or bridge connection.
"""
from __future__ import annotations

import unittest

from scripts.isaac_keyboard_teleop import _apply_key


_VX_MAX = 0.5
_YAW_MAX = 1.0
_HEIGHT_MAX = 0.03


class TestKeyMapping(unittest.TestCase):
    """_apply_key maps W/S/A/D/Q/E to velocity/yaw/height."""

    def test_w_increases_vx(self) -> None:
        vx, yaw, h, quit = _apply_key("w", 0.0, 0.0, 0.0, _VX_MAX, _YAW_MAX, _HEIGHT_MAX)
        self.assertGreater(vx, 0.0)
        self.assertFalse(quit)

    def test_s_decreases_vx(self) -> None:
        vx, yaw, h, quit = _apply_key("s", 0.0, 0.0, 0.0, _VX_MAX, _YAW_MAX, _HEIGHT_MAX)
        self.assertLess(vx, 0.0)
        self.assertFalse(quit)

    def test_a_increases_yaw(self) -> None:
        vx, yaw, h, quit = _apply_key("a", 0.0, 0.0, 0.0, _VX_MAX, _YAW_MAX, _HEIGHT_MAX)
        self.assertGreater(yaw, 0.0)
        self.assertFalse(quit)

    def test_d_decreases_yaw(self) -> None:
        vx, yaw, h, quit = _apply_key("d", 0.0, 0.0, 0.0, _VX_MAX, _YAW_MAX, _HEIGHT_MAX)
        self.assertLess(yaw, 0.0)
        self.assertFalse(quit)

    def test_q_increases_height(self) -> None:
        vx, yaw, h, quit = _apply_key("q", 0.0, 0.0, 0.0, _VX_MAX, _YAW_MAX, _HEIGHT_MAX)
        self.assertGreater(h, 0.0)
        self.assertFalse(quit)

    def test_e_decreases_height(self) -> None:
        vx, yaw, h, quit = _apply_key("e", 0.0, 0.0, 0.0, _VX_MAX, _YAW_MAX, _HEIGHT_MAX)
        self.assertLess(h, 0.0)
        self.assertFalse(quit)

    def test_space_zeroes_all(self) -> None:
        vx, yaw, h, quit = _apply_key(" ", 0.3, 0.5, 0.01, _VX_MAX, _YAW_MAX, _HEIGHT_MAX)
        self.assertAlmostEqual(vx, 0.0)
        self.assertAlmostEqual(yaw, 0.0)
        self.assertAlmostEqual(h, 0.0)
        self.assertFalse(quit)

    def test_esc_quits(self) -> None:
        vx, yaw, h, quit = _apply_key("ESC", 0.3, 0.5, 0.01, _VX_MAX, _YAW_MAX, _HEIGHT_MAX)
        self.assertTrue(quit)

    def test_ctrl_c_quits(self) -> None:
        vx, yaw, h, quit = _apply_key("\x03", 0.0, 0.0, 0.0, _VX_MAX, _YAW_MAX, _HEIGHT_MAX)
        self.assertTrue(quit)

    def test_unknown_key_noop(self) -> None:
        vx, yaw, h, quit = _apply_key("x", 0.3, 0.5, 0.01, _VX_MAX, _YAW_MAX, _HEIGHT_MAX)
        self.assertAlmostEqual(vx, 0.3)
        self.assertAlmostEqual(yaw, 0.5)
        self.assertAlmostEqual(h, 0.01)
        self.assertFalse(quit)

    def test_case_insensitive(self) -> None:
        vx1, _, _, _ = _apply_key("W", 0.0, 0.0, 0.0, _VX_MAX, _YAW_MAX, _HEIGHT_MAX)
        vx2, _, _, _ = _apply_key("w", 0.0, 0.0, 0.0, _VX_MAX, _YAW_MAX, _HEIGHT_MAX)
        self.assertAlmostEqual(vx1, vx2)


class TestKeyClamping(unittest.TestCase):
    """Key presses clamp to configured maxima."""

    def test_vx_clamped_at_max(self) -> None:
        vx = 0.0
        for _ in range(100):
            vx, _, _, _ = _apply_key("w", vx, 0.0, 0.0, _VX_MAX, _YAW_MAX, _HEIGHT_MAX)
        self.assertAlmostEqual(vx, _VX_MAX)

    def test_vx_clamped_at_negative_max(self) -> None:
        vx = 0.0
        for _ in range(100):
            vx, _, _, _ = _apply_key("s", vx, 0.0, 0.0, _VX_MAX, _YAW_MAX, _HEIGHT_MAX)
        self.assertAlmostEqual(vx, -_VX_MAX)

    def test_yaw_clamped(self) -> None:
        yaw = 0.0
        for _ in range(100):
            _, yaw, _, _ = _apply_key("a", 0.0, yaw, 0.0, _VX_MAX, _YAW_MAX, _HEIGHT_MAX)
        self.assertAlmostEqual(yaw, _YAW_MAX)

    def test_height_clamped(self) -> None:
        h = 0.0
        for _ in range(100):
            _, _, h, _ = _apply_key("q", 0.0, 0.0, h, _VX_MAX, _YAW_MAX, _HEIGHT_MAX)
        self.assertAlmostEqual(h, _HEIGHT_MAX)


class TestModuleImport(unittest.TestCase):
    """Verify the script module can be imported without side effects."""

    def test_import_bridge_connection(self) -> None:
        from scripts.isaac_keyboard_teleop import BridgeConnection
        self.assertTrue(callable(BridgeConnection))

    def test_import_main(self) -> None:
        from scripts.isaac_keyboard_teleop import main
        self.assertTrue(callable(main))


if __name__ == "__main__":
    unittest.main()
