"""Regression tests for ``server.airframes.multicopter``.

Each test corresponds to a bug we shipped during the camera-drone debug
session — keep them green and the next drone build won't repeat the
afternoon.
"""
from __future__ import annotations

import tempfile
import unittest
import xml.etree.ElementTree as ET

from server.airframes import Box, StructuralBody
from server.airframes.multicopter import MulticopterAirframe, Rotor
from server.airframes.presets import cinema_drone, x500_like


class TestHoverThrottle(unittest.TestCase):
    """Bug E (regression): hover throttle must use quadratic-correct math."""

    def test_x500_like_lands_in_x500_ballpark(self) -> None:
        # x500's empirically-tuned MPC_THR_HOVER is 0.60.  Our
        # physics-only formula (no aero drag) gives ~0.73 — within 25 %
        # of the empirical value, which is the right ballpark for a
        # baseline that PX4's outer loop trims.
        af = x500_like()
        h = af.hover_throttle()
        self.assertGreater(h, 0.5)
        self.assertLess(h, 0.85)

    def test_cinema_drone_uses_high_throttle_band(self) -> None:
        # 5.7 kg drone with cinema-class motors lives at ~0.65 hover.
        # The legacy linear formula gave 0.49 — too low; PX4 was
        # perpetually under-throttled during AUTO_TAKEOFF.
        af = cinema_drone()
        h = af.hover_throttle()
        self.assertGreater(h, 0.55)
        self.assertLess(h, 0.85)

    def test_too_heavy_raises(self) -> None:
        chassis = StructuralBody("chassis", 50.0, Box((0.3, 0.3, 0.1)))
        af = MulticopterAirframe(
            name="too_heavy",
            chassis=chassis,
            rotors=tuple(
                Rotor(name=f"r{i}", position_m=(0.13, 0.13, 0), direction="ccw")
                for i in range(4)
            ),
        )
        with self.assertRaises(ValueError) as ctx:
            af.hover_throttle()
        self.assertIn("too heavy", str(ctx.exception).lower())

    def test_too_light_raises(self) -> None:
        chassis = StructuralBody("chassis", 0.001, Box((0.05, 0.05, 0.01)))
        af = MulticopterAirframe(
            name="too_light",
            chassis=chassis,
            rotors=tuple(
                Rotor(name=f"r{i}", position_m=(0.05, 0.05, 0),
                      direction="ccw" if i % 2 == 0 else "cw",
                      mass_kg=0.001)
                for i in range(4)
            ),
        )
        with self.assertRaises(ValueError) as ctx:
            af.hover_throttle()
        self.assertIn("too light", str(ctx.exception).lower())


class TestChassisInertiaAggregation(unittest.TestCase):
    """Bug F (regression): chassis inertia must include structural bodies."""

    def test_chassis_only_vs_with_structural_bodies(self) -> None:
        chassis = StructuralBody("chassis", 0.30, Box((0.20, 0.20, 0.03)))
        chassis_only = MulticopterAirframe(
            name="bare",
            chassis=chassis,
            rotors=_quad_rotors(),
        )
        m_bare, _, i_bare = chassis_only.chassis_inertia()
        self.assertAlmostEqual(m_bare, 0.30, places=4)

        with_bodies = MulticopterAirframe(
            name="loaded",
            chassis=chassis,
            structural_bodies=(
                StructuralBody("battery", 1.5, Box((0.10, 0.08, 0.04)),
                               com_offset_m=(0, 0, 0.05)),
                StructuralBody("payload", 2.5, Box((0.10, 0.08, 0.06)),
                               com_offset_m=(0, 0, -0.04)),
            ),
            rotors=_quad_rotors(),
        )
        m_loaded, _, i_loaded = with_bodies.chassis_inertia()
        self.assertAlmostEqual(m_loaded, 0.30 + 1.5 + 2.5, places=4)
        # Aggregate inertia must be MUCH larger than chassis-alone.
        # Parallel-axis from the offset bodies plus their own ixx adds
        # at least a factor of 5×.
        self.assertGreater(i_loaded.ixx, 5 * i_bare.ixx)


class TestSimModelEmission(unittest.TestCase):
    """Build a SimModel through the spec and assert the right shape."""

    def test_to_sim_model_links_and_joints(self) -> None:
        af = x500_like()
        sm = af.to_sim_model()
        self.assertEqual(sm.name, "x500_like")
        # 1 chassis + 4 rotors
        self.assertEqual(len(sm.links), 5)
        self.assertEqual(len(sm.joints), 4)
        # Every joint connects chassis to a rotor with axis (0, 0, 1)
        for jt in sm.joints:
            self.assertEqual(jt.parent, "base_link")
            self.assertEqual(jt.axis, (0.0, 0.0, 1.0))
            self.assertEqual(jt.joint_type, "continuous")

    def test_rotor_links_use_thin_disk_inertia(self) -> None:
        """Bug G regression: rotor inertia must be thin-disk-like."""
        af = x500_like()
        sm = af.to_sim_model()
        for link in sm.links:
            if link.name.startswith("rotor"):
                ixx, _, _, iyy, _, izz = link.inertia
                # Thin disk about Z: ixx == iyy and izz ≈ 2·ixx
                self.assertAlmostEqual(ixx, iyy, places=9)
                self.assertGreater(izz, ixx * 1.9)
                self.assertLess(izz, ixx * 2.1)

    def test_rotor_collision_is_primitive_cylinder(self) -> None:
        """Bug H regression: avoid mesh collision (DART crashes)."""
        af = x500_like()
        sm = af.to_sim_model()
        for link in sm.links:
            if link.name.startswith("rotor"):
                self.assertIsNotNone(link.collision_shape)
                self.assertEqual(link.collision_shape.kind, "cylinder")

    def test_chassis_collision_is_primitive_box(self) -> None:
        af = x500_like()
        sm = af.to_sim_model()
        chassis = next(link for link in sm.links if link.name == "base_link")
        self.assertIsNotNone(chassis.collision_shape)
        self.assertEqual(chassis.collision_shape.kind, "box")


class TestSDFEmission(unittest.TestCase):
    """End-to-end: spec → SimModel → SDF, assert the bug-fixed tags."""

    def _write_sdf(self, af: MulticopterAirframe) -> str:
        from server.sim_export import write_sdf
        sm = af.to_sim_model()
        with tempfile.NamedTemporaryFile(suffix=".sdf", delete=False) as f:
            path = f.name
        # drone_config tells write_sdf to emit motor plugins
        cfg = {
            "rotors": [
                {"index": i, "joint": f"{r.name}_joint",
                 "direction": r.direction, "link": r.name}
                for i, r in enumerate(af.rotors)
            ],
            "sensors": False,   # keep test fast; sensors tested elsewhere
        }
        write_sdf(sm, path, drone_config=cfg)
        return path

    def test_no_joint_pose_in_sdf(self) -> None:
        """Bug 5 regression: joint <pose> would double-offset rotation axis."""
        path = self._write_sdf(x500_like())
        tree = ET.parse(path)
        for joint_el in tree.iter("joint"):
            self.assertIsNone(
                joint_el.find("pose"),
                f"Joint {joint_el.attrib.get('name')} must omit <pose> "
                "(SDF 1.10 default = child link frame, which is the joint origin)",
            )

    def test_motor_plugin_uses_motor_number_and_velocity(self) -> None:
        """Bugs C-7, C-8, C-9 regression: gz-sim motor plugin tag set."""
        path = self._write_sdf(x500_like())
        tree = ET.parse(path)
        plugins = [
            p for p in tree.iter("plugin")
            if "MulticopterMotorModel" in (p.attrib.get("name") or "")
        ]
        self.assertEqual(len(plugins), 4)
        for p in plugins:
            self.assertIsNotNone(p.find("motorNumber"),
                                 "must use <motorNumber>, not <actuator_number>")
            self.assertIsNone(p.find("actuator_number"),
                              "<actuator_number> is the wrong tag for gz-sim")
            self.assertIsNone(p.find("robotNamespace"),
                              "<robotNamespace> breaks PX4 topic match")
            mt = p.find("motorType")
            self.assertIsNotNone(mt, "missing <motorType>velocity</motorType>")
            self.assertEqual(mt.text, "velocity")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _quad_rotors() -> tuple[Rotor, ...]:
    return (
        Rotor(name="rotor_FL", position_m=(+0.13, +0.22, 0.06), direction="ccw"),
        Rotor(name="rotor_FR", position_m=(+0.13, -0.22, 0.06), direction="cw"),
        Rotor(name="rotor_RR", position_m=(-0.13, -0.22, 0.06), direction="ccw"),
        Rotor(name="rotor_RL", position_m=(-0.13, +0.22, 0.06), direction="cw"),
    )


if __name__ == "__main__":
    unittest.main()
