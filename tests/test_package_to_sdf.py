"""Tests for gazebo_bridge.package_to_sdf — the manifest → SDF compiler.

Core writes a neutral package; Gazebo's dialect is produced here, at load
time.  These tests pin the compiled output against the SDF shape gz-sim and
PX4 actually accept, and feed the compiler manifests built by core's own
builder so the two stay in step.
"""

from __future__ import annotations

import json
import math
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from gazebo_bridge.package_to_sdf import (
    SdfCompileError,
    compile_and_validate,
    compile_package_to_sdf,
    load_manifest,
    validate_sdf,
)


def _manifest(**overrides: Any) -> dict[str, Any]:
    """A two-link drone-ish manifest: chassis + one rotor."""
    manifest: dict[str, Any] = {
        "schema_version": "1.0.0",
        "name": "test_quad",
        "generator": "test",
        "mode": "full",
        "units": {"length": "m", "mass": "kg", "angle": "rad"},
        "links": [
            {
                "name": "chassis",
                "is_root": True,
                "mesh": {
                    "file": "chassis.stl",
                    "unit": "mm",
                    "scale_to_m": 0.001,
                    "frame": "link_local",
                },
                "world_pose": {"xyz_m": [0.0, 0.0, 0.0], "quat_wxyz": [1.0, 0.0, 0.0, 0.0]},
                "mass_kg": 1.2,
                "inertia": {
                    "ixx": 0.01,
                    "ixy": 0.0,
                    "ixz": 0.0,
                    "iyy": 0.01,
                    "iyz": 0.0,
                    "izz": 0.02,
                },
            },
            {
                "name": "rotor_0",
                "world_pose": {"xyz_m": [0.15, 0.15, 0.05], "quat_wxyz": [1.0, 0.0, 0.0, 0.0]},
                "mass_kg": 0.016,
                "collision": {"kind": "cylinder", "radius_m": 0.1, "length_m": 0.005},
            },
        ],
        "joints": [
            {
                "name": "rotor_0_joint",
                "type": "continuous",
                "parent": "chassis",
                "child": "rotor_0",
                "origin_xyz_m": [0.15, 0.15, 0.05],
                "origin_rpy_rad": [0.0, 0.0, 0.0],
                "axis": [0.0, 0.0, 1.0],
                "effort": 10.0,
                "velocity": 1000.0,
                "damping": 0.004,
                "friction": 0.0,
            }
        ],
        "actuators": [
            {
                "type": "rotor",
                "name": "rotor_0",
                "index": 0,
                "joint": "rotor_0_joint",
                "link": "rotor_0",
                "position_m": [0.15, 0.15, 0.05],
                "direction": "ccw",
                "max_thrust_N": 8.55,
                "moment_constant": 0.016,
                "motor_constant": 8.54858e-06,
                "max_rot_velocity_rad_s": 1000.0,
            }
        ],
        "sensors": [
            {"type": "imu", "link": "chassis"},
            {"type": "gps", "link": "chassis"},
        ],
    }
    manifest.update(overrides)
    return manifest


class _Package:
    """Write a manifest (and a dummy mesh) into a temp package directory."""

    def __init__(self, manifest: dict[str, Any]) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        (self.dir / "chassis.stl").write_text("solid x\nendsolid x\n")
        (self.dir / "manifest.json").write_text(json.dumps(manifest))

    def cleanup(self) -> None:
        self._tmp.cleanup()


class _CompilerTestCase(unittest.TestCase):
    def compile(self, manifest: dict[str, Any] | None = None) -> ET.Element:
        pkg = _Package(manifest or _manifest())
        self.addCleanup(pkg.cleanup)
        sdf_path = compile_package_to_sdf(str(pkg.dir))
        self.pkg_dir = pkg.dir
        self.sdf_path = sdf_path
        return ET.parse(sdf_path).getroot()


class TestStructure(_CompilerTestCase):
    def test_model_and_links(self) -> None:
        root = self.compile()
        self.assertEqual(root.tag, "sdf")
        self.assertEqual(root.get("version"), "1.10")
        model = root.find("model")
        self.assertEqual(model.get("name"), "test_quad")
        self.assertEqual([lk.get("name") for lk in model.findall("link")], ["chassis", "rotor_0"])

    def test_output_lands_beside_the_manifest(self) -> None:
        self.compile()
        self.assertEqual(Path(self.sdf_path).parent, self.pkg_dir)
        self.assertEqual(Path(self.sdf_path).name, "test_quad.sdf")

    def test_link_pose_is_world_pose_in_metres(self) -> None:
        model = self.compile().find("model")
        rotor = next(lk for lk in model.findall("link") if lk.get("name") == "rotor_0")
        values = [float(v) for v in rotor.findtext("pose").split()]
        self.assertAlmostEqual(values[0], 0.15)
        self.assertAlmostEqual(values[1], 0.15)
        self.assertAlmostEqual(values[2], 0.05)

    def test_quaternion_becomes_rpy(self) -> None:
        manifest = _manifest()
        # 90° about Z as a quaternion.
        manifest["links"][1]["world_pose"]["quat_wxyz"] = [
            math.cos(math.pi / 4),
            0.0,
            0.0,
            math.sin(math.pi / 4),
        ]
        model = self.compile(manifest).find("model")
        rotor = next(lk for lk in model.findall("link") if lk.get("name") == "rotor_0")
        yaw = float(rotor.findtext("pose").split()[5])
        self.assertAlmostEqual(yaw, math.pi / 2, places=5)

    def test_mesh_uri_is_absolute_with_declared_scale(self) -> None:
        model = self.compile().find("model")
        chassis = next(lk for lk in model.findall("link") if lk.get("name") == "chassis")
        mesh = chassis.find("visual/geometry/mesh")
        self.assertTrue(Path(mesh.findtext("uri")).is_absolute())
        self.assertEqual(Path(mesh.findtext("uri")).name, "chassis.stl")
        self.assertEqual(mesh.findtext("scale"), "0.001 0.001 0.001")

    def test_inertial_from_manifest(self) -> None:
        model = self.compile().find("model")
        chassis = next(lk for lk in model.findall("link") if lk.get("name") == "chassis")
        self.assertAlmostEqual(float(chassis.findtext("inertial/mass")), 1.2)
        self.assertAlmostEqual(float(chassis.findtext("inertial/inertia/izz")), 0.02)

    def test_primitive_collision_gets_a_matching_visual(self) -> None:
        """A mesh-less link still renders — otherwise the drone is invisible."""
        model = self.compile().find("model")
        rotor = next(lk for lk in model.findall("link") if lk.get("name") == "rotor_0")
        self.assertIsNotNone(rotor.find("visual/geometry/cylinder"))
        self.assertIsNotNone(rotor.find("collision/geometry/cylinder"))
        self.assertAlmostEqual(float(rotor.findtext("collision/geometry/cylinder/radius")), 0.1)

    def test_mesh_link_without_primitive_falls_back_to_mesh_collision(self) -> None:
        model = self.compile().find("model")
        chassis = next(lk for lk in model.findall("link") if lk.get("name") == "chassis")
        self.assertIsNotNone(chassis.find("collision/geometry/mesh"))


class TestJoints(_CompilerTestCase):
    def test_continuous_becomes_revolute(self) -> None:
        model = self.compile().find("model")
        joint = model.find("joint")
        self.assertEqual(joint.get("type"), "revolute")
        self.assertEqual(joint.findtext("parent"), "chassis")
        self.assertEqual(joint.findtext("child"), "rotor_0")

    def test_no_joint_pose_is_emitted(self) -> None:
        """SDF 1.10 defaults the joint frame to the child link.

        Writing the origin again double-offsets the axis — the rotor spins
        about a point away from its hub.
        """
        model = self.compile().find("model")
        self.assertIsNone(model.find("joint").find("pose"))

    def test_limits_and_dynamics(self) -> None:
        manifest = _manifest()
        manifest["joints"][0]["type"] = "revolute"
        manifest["joints"][0]["limits"] = {"lower": -1.5, "upper": 1.5}
        model = self.compile(manifest).find("model")
        axis = model.find("joint/axis")
        self.assertAlmostEqual(float(axis.findtext("limit/lower")), -1.5)
        self.assertAlmostEqual(float(axis.findtext("limit/effort")), 10.0)
        self.assertAlmostEqual(float(axis.findtext("dynamics/damping")), 0.004)


class TestMotorPlugins(_CompilerTestCase):
    def test_one_canonical_plugin_per_rotor(self) -> None:
        model = self.compile().find("model")
        plugins = model.findall("plugin")
        self.assertEqual(len(plugins), 1)
        plugin = plugins[0]
        self.assertEqual(plugin.get("filename"), "gz-sim-multicopter-motor-model-system")
        self.assertEqual(plugin.get("name"), "gz::sim::systems::MulticopterMotorModel")
        self.assertEqual(plugin.findtext("jointName"), "rotor_0_joint")
        self.assertEqual(plugin.findtext("linkName"), "rotor_0")
        self.assertEqual(plugin.findtext("turningDirection"), "ccw")
        self.assertEqual(plugin.findtext("motorNumber"), "0")
        self.assertEqual(plugin.findtext("commandSubTopic"), "command/motor_speed")
        self.assertEqual(plugin.findtext("motorType"), "velocity")
        # <robotNamespace> would break the topic match with PX4's gz_bridge.
        self.assertIsNone(plugin.find("robotNamespace"))
        # No <rotor> children — that shape is rejected by gz-sim and PX4.
        self.assertEqual(plugin.findall("rotor"), [])

    def test_motor_constant_recovered_from_thrust_when_absent(self) -> None:
        manifest = _manifest()
        del manifest["actuators"][0]["motor_constant"]
        manifest["actuators"][0]["max_thrust_N"] = 10.0
        manifest["actuators"][0]["max_rot_velocity_rad_s"] = 1000.0
        model = self.compile(manifest).find("model")
        # k = F / w^2 = 10 / 1e6
        self.assertAlmostEqual(float(model.findtext("plugin/motorConstant")), 1e-05)

    def test_rotor_without_a_link_is_skipped(self) -> None:
        manifest = _manifest()
        manifest["actuators"][0].pop("link")
        manifest["actuators"][0]["joint"] = "unknown_joint"
        model = self.compile(manifest).find("model")
        self.assertEqual(model.findall("plugin"), [])

    def test_no_actuators_means_no_plugins(self) -> None:
        manifest = _manifest()
        manifest.pop("actuators")
        model = self.compile(manifest).find("model")
        self.assertEqual(model.findall("plugin"), [])


class TestSensors(_CompilerTestCase):
    def test_sensors_land_on_their_link(self) -> None:
        model = self.compile().find("model")
        chassis = next(lk for lk in model.findall("link") if lk.get("name") == "chassis")
        types = {s.get("type") for s in chassis.findall("sensor")}
        self.assertEqual(types, {"imu", "navsat"})

    def test_no_sensors_declared(self) -> None:
        manifest = _manifest()
        manifest.pop("sensors")
        model = self.compile(manifest).find("model")
        self.assertEqual(model.findall("link/sensor"), [])


class TestValidation(unittest.TestCase):
    def _write(self, xml: str) -> str:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "model.sdf"
        path.write_text(xml)
        return str(path)

    def test_clean_package_has_no_findings(self) -> None:
        pkg = _Package(_manifest())
        self.addCleanup(pkg.cleanup)
        compiled = compile_and_validate(str(pkg.dir))
        self.assertEqual(compiled["findings"], [])
        self.assertTrue(compiled["drone_mode"])
        self.assertEqual(compiled["model_name"], "test_quad")

    def test_dangling_joint_endpoint_blocks(self) -> None:
        path = self._write(
            '<sdf version="1.10"><model name="m">'
            '<link name="a"/><joint name="j" type="fixed">'
            "<parent>a</parent><child>ghost</child></joint>"
            "</model></sdf>"
        )
        findings = validate_sdf(path)
        codes = {f["rule_id"] for f in findings}
        self.assertIn("sdf.dangling_child", codes)
        self.assertTrue(all(f["severity"] in ("block", "warn") for f in findings))

    def test_drone_mode_wants_a_plugin(self) -> None:
        path = self._write(
            '<sdf version="1.10"><model name="m">'
            '<link name="a"/><link name="b"/>'
            '<joint name="j" type="fixed"><parent>a</parent><child>b</child></joint>'
            "</model></sdf>"
        )
        findings = validate_sdf(path, drone_mode=True)
        self.assertIn("sdf.drone.plugin_missing", {f["rule_id"] for f in findings})

    def test_parse_error_is_a_finding_not_an_exception(self) -> None:
        path = self._write("<sdf><model>")
        findings = validate_sdf(path)
        self.assertEqual(findings[0]["rule_id"], "sdf.parse_error")


class TestManifestLoading(unittest.TestCase):
    def test_missing_package_raises_package_invalid(self) -> None:
        with self.assertRaises(SdfCompileError) as ctx:
            load_manifest("/nonexistent/package")
        self.assertEqual(ctx.exception.code, "PACKAGE_INVALID")

    def test_manifest_without_links_is_rejected(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        (Path(tmp.name) / "manifest.json").write_text(json.dumps({"name": "x", "links": []}))
        with self.assertRaises(SdfCompileError):
            load_manifest(tmp.name)


class TestCoreManifestCompiles(unittest.TestCase):
    """The compiler ingests what core's builder actually writes.

    This is the seam the dialect inversion turns on: if core changes the
    manifest and the bridge is not updated, this fails.
    """

    def test_reduced_and_full_manifests_compile(self) -> None:
        from server.sim_export import build_sim_model
        from server.sim_package_manifest import build_manifest, write_manifest
        from tests.test_engine_contract import _body_manifest, _mechanism

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmp_path = Path(tmp.name)

        bodies = _body_manifest(tmp_path)
        reduced = build_manifest(name="reduced_pkg", output_dir=str(tmp_path), body_manifest=bodies)
        write_manifest(reduced, str(tmp_path))
        sdf_path = compile_package_to_sdf(str(tmp_path))
        model = ET.parse(sdf_path).getroot().find("model")
        self.assertEqual(len(model.findall("link")), 2)
        self.assertEqual(model.findall("joint"), [])

        model_obj = build_sim_model(_mechanism(), bodies)
        full = build_manifest(
            name=model_obj.name,
            output_dir=str(tmp_path),
            body_manifest=bodies,
            sim_model=model_obj,
            drone_config={
                "rotors": [{"index": 0, "joint": "arm_joint", "direction": "cw"}],
                "sensors": {"imu": True, "gps": False, "barometer": False, "magnetometer": False},
            },
        )
        write_manifest(full, str(tmp_path))
        compiled = compile_and_validate(str(tmp_path))
        self.assertEqual(compiled["findings"], [])
        full_model = ET.parse(compiled["sdf_path"]).getroot().find("model")
        self.assertEqual(len(full_model.findall("joint")), 1)
        self.assertEqual(len(full_model.findall("plugin")), 1)
        self.assertEqual(full_model.findtext("plugin/turningDirection"), "cw")


if __name__ == "__main__":
    unittest.main()
