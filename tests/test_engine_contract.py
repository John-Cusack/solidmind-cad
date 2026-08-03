"""Engine Integration Contract v1 conformance — docs/engine-contract.md.

Covers the three things step 1 of the engine migration publishes:

1. the ``hello`` handshake + ``request_id`` passthrough on the reference
   (Gazebo stub) bridge,
2. ``simulate`` results validating against ``schemas/sim_result.schema.json``,
3. the canonical package manifest validating against
   ``schemas/sim_package.schema.json`` in both full and reduced modes.
"""

from __future__ import annotations

import json
import socket
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import jsonschema

from server.motion_models import JointEdge, JointType, Mechanism, PartNode
from server.sim_export import build_sim_model
from server.sim_package_manifest import build_manifest, write_manifest
from server.tools_cad import cad_export_sim_package
from tests.conftest import ReferenceEngineFixture, mechanism_factory, unused_tcp_port

_SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas"


def _load_schema(name: str) -> dict[str, Any]:
    with open(_SCHEMA_DIR / name) as fh:
        return json.load(fh)


def _send_raw(host: str, port: int, payload: dict[str, Any]) -> dict[str, Any]:
    """Send one raw envelope (no client wrapper) and return the response."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(10.0)
    sock.connect((host, port))
    try:
        sock.sendall((json.dumps(payload) + "\n").encode())
        data = b""
        while b"\n" not in data:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
        return json.loads(data.decode().strip())
    finally:
        sock.close()


# Minimal valid ASCII STL — build_sim_model rewrites meshes in place, so the
# full-mode manifest tests need real files on disk.
_STL_TEMPLATE = """solid test
  facet normal 0 0 -1
    outer loop
      vertex 0 0 0
      vertex 10 0 0
      vertex 0 10 0
    endloop
  endfacet
  facet normal 0 0 1
    outer loop
      vertex 0 0 10
      vertex 10 0 10
      vertex 0 10 10
    endloop
  endfacet
endsolid test
"""


def _write_stl(path: Path) -> str:
    path.write_text(_STL_TEMPLATE)
    return str(path)


class TestHelloHandshake(unittest.TestCase):
    """hello answers truthfully and request_id round-trips (contract §1, §2)."""

    def test_hello_shape(self) -> None:
        port = unused_tcp_port()
        with ReferenceEngineFixture(port) as bridge:
            resp = _send_raw(bridge.host, bridge.port, {"cmd": "hello", "args": {}})

        self.assertTrue(resp["ok"], resp)
        result = resp["result"]
        self.assertEqual(result["protocol_version"], "1.0.0")
        self.assertEqual(result["contract_versions_supported"], ["1"])
        self.assertEqual(result["engine"], "reference")
        self.assertIsInstance(result["engine_version"], str)
        self.assertEqual(result["runtime_mode"], "real")

        caps = result["capabilities"]
        self.assertEqual(caps["modes"], ["batch", "session", "teleop"])
        self.assertEqual(caps["formats"], ["package", "mechanism", "urdf"])
        self.assertEqual(caps["features"], ["diagnose"])
        self.assertEqual(caps["fields"], {"emits": [], "accepts": []})

    def test_advertised_capabilities_have_handlers(self) -> None:
        """Capability honesty: every advertised verb group actually answers."""
        port = unused_tcp_port()
        with ReferenceEngineFixture(port) as bridge:
            hello = _send_raw(bridge.host, bridge.port, {"cmd": "hello"})["result"]
            # features -> one representative verb each
            for verb in ("ping", "diagnose"):
                resp = _send_raw(bridge.host, bridge.port, {"cmd": verb, "args": {}})
                self.assertTrue(resp["ok"], f"{verb}: {resp}")
        self.assertIn("diagnose", hello["capabilities"]["features"])
        self.assertIn("session", hello["capabilities"]["modes"])

    def test_request_id_echoed_when_sent(self) -> None:
        port = unused_tcp_port()
        with ReferenceEngineFixture(port) as bridge:
            resp = _send_raw(
                bridge.host,
                bridge.port,
                {"cmd": "hello", "args": {}, "request_id": "req-42"},
            )
        self.assertEqual(resp["request_id"], "req-42")

    def test_request_id_absent_when_not_sent(self) -> None:
        port = unused_tcp_port()
        with ReferenceEngineFixture(port) as bridge:
            resp = _send_raw(bridge.host, bridge.port, {"cmd": "ping", "args": {}})
        self.assertNotIn("request_id", resp)

    def test_request_id_echoed_on_error(self) -> None:
        port = unused_tcp_port()
        with ReferenceEngineFixture(port) as bridge:
            resp = _send_raw(
                bridge.host,
                bridge.port,
                {"cmd": "definitely_not_a_verb", "request_id": "req-err"},
            )
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["request_id"], "req-err")

    def test_unknown_command_code(self) -> None:
        port = unused_tcp_port()
        with ReferenceEngineFixture(port) as bridge:
            resp = _send_raw(bridge.host, bridge.port, {"cmd": "definitely_not_a_verb"})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["code"], "UNSUPPORTED_COMMAND")


class TestSimResultSchema(unittest.TestCase):
    """The reference bridge's simulate result matches the published schema."""

    def test_stub_simulate_validates(self) -> None:
        schema = _load_schema("sim_result.schema.json")
        port = unused_tcp_port()
        with ReferenceEngineFixture(port) as bridge:
            resp = _send_raw(
                bridge.host,
                bridge.port,
                {
                    "cmd": "simulate",
                    "args": {
                        "mechanism": mechanism_factory("gear_pair"),
                        "duration_s": 1.0,
                        "dt_s": 0.01,
                        "output_interval": 0.1,
                    },
                },
            )
        self.assertTrue(resp["ok"], resp)
        jsonschema.validate(resp["result"], schema)

    def test_tier35_members_are_shape_pinned(self) -> None:
        """peak_joint_forces / joint_efforts are what analysis_sim_coupling reads."""
        schema = _load_schema("sim_result.schema.json")
        bad = {
            "time_series": [{"t": 0.0, "joint_efforts": ["not-a-number"]}],
            "summary": {"simulation_time_s": 1.0, "dt_s": 0.01, "engine_mode": "stub"},
        }
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(bad, schema)

        bad_summary = {
            "time_series": [],
            "summary": {
                "simulation_time_s": 1.0,
                "dt_s": 0.01,
                "engine_mode": "stub",
                "peak_joint_forces": {"j1": "heavy"},
            },
        }
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(bad_summary, schema)

    def test_summary_requires_contract_keys(self) -> None:
        schema = _load_schema("sim_result.schema.json")
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate({"time_series": [], "summary": {}}, schema)


class TestFieldSnapshotSchema(unittest.TestCase):
    def test_sidecar_validates(self) -> None:
        schema = _load_schema("field_snapshot.schema.json")
        jsonschema.validate(
            {
                "schema_version": "1.0.0",
                "quantity": "temperature",
                "units": "K",
                "mesh_ref": "rotor_hub.vtu",
                "time_s": None,
                "data_file": "rotor_hub_temperature_steady.vtu",
            },
            schema,
        )

    def test_quantity_vocabulary_is_closed(self) -> None:
        schema = _load_schema("field_snapshot.schema.json")
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(
                {
                    "schema_version": "1.0.0",
                    "quantity": "vibes",
                    "units": "K",
                    "mesh_ref": "m.vtu",
                    "time_s": 0.0,
                    "data_file": "d.vtu",
                },
                schema,
            )


def _body_manifest(tmp: Path) -> list[dict[str, Any]]:
    """Two bodies, 100 mm apart in Z — exercises the mm → m conversion."""
    return [
        {
            "name": "Body_Base",
            "label": "Base",
            "mesh_path": _write_stl(tmp / "Body_Base.stl"),
            "placement": {"position": [0.0, 0.0, 0.0], "rotation_quat": [1.0, 0.0, 0.0, 0.0]},
            "bbox_mm": [20.0, 20.0, 10.0],
            "bbox_min_mm": [-10.0, -10.0, 0.0],
            "volume_mm3": 4000.0,
        },
        {
            "name": "Body_Arm",
            "label": "Arm",
            "mesh_path": _write_stl(tmp / "Body_Arm.stl"),
            "placement": {"position": [0.0, 0.0, 100.0], "rotation_quat": [1.0, 0.0, 0.0, 0.0]},
            "bbox_mm": [10.0, 10.0, 40.0],
            "bbox_min_mm": [-5.0, -5.0, 0.0],
            "volume_mm3": 4000.0,
        },
    ]


def _mechanism() -> Mechanism:
    return Mechanism(
        name="test_arm",
        parts=(
            PartNode(id="base", body_name="Body_Base", is_ground=True),
            PartNode(id="arm", body_name="Body_Arm"),
        ),
        joints=(
            JointEdge(
                id="arm_joint",
                joint_type=JointType.REVOLUTE,
                parent_part="base",
                child_part="arm",
                origin=(0.0, 0.0, 100.0),
                axis=(0.0, 0.0, 1.0),
                min_angle_deg=-90.0,
                max_angle_deg=90.0,
            ),
        ),
        drives=(),
    )


class TestSimPackageManifest(unittest.TestCase):
    """The manifest builder — contract §6, schemas/sim_package.schema.json."""

    def setUp(self) -> None:
        self.schema = _load_schema("sim_package.schema.json")
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_reduced_mode_validates(self) -> None:
        manifest = build_manifest(
            name="bodies_only",
            output_dir=str(self.tmp),
            body_manifest=_body_manifest(self.tmp),
        )
        jsonschema.validate(manifest, self.schema)
        self.assertEqual(manifest["mode"], "reduced")
        self.assertEqual(manifest["joints"], [])
        self.assertEqual(len(manifest["links"]), 2)

    def test_reduced_mode_converts_mm_to_m(self) -> None:
        manifest = build_manifest(
            name="bodies_only",
            output_dir=str(self.tmp),
            body_manifest=_body_manifest(self.tmp),
        )
        arm = next(lk for lk in manifest["links"] if lk["name"] == "Body_Arm")
        self.assertAlmostEqual(arm["world_pose"]["xyz_m"][2], 0.1)  # 100 mm -> 0.1 m
        self.assertEqual(arm["world_pose"]["quat_wxyz"], [1.0, 0.0, 0.0, 0.0])

    def test_reduced_mode_mesh_is_relative_and_world_framed(self) -> None:
        manifest = build_manifest(
            name="bodies_only",
            output_dir=str(self.tmp),
            body_manifest=_body_manifest(self.tmp),
        )
        mesh = manifest["links"][0]["mesh"]
        self.assertEqual(mesh["file"], "Body_Base.stl")  # relative to the package dir
        self.assertEqual(mesh["unit"], "mm")
        self.assertEqual(mesh["scale_to_m"], 0.001)
        self.assertEqual(mesh["frame"], "world")

    def test_units_block_is_si(self) -> None:
        manifest = build_manifest(
            name="bodies_only",
            output_dir=str(self.tmp),
            body_manifest=_body_manifest(self.tmp),
        )
        self.assertEqual(manifest["units"], {"length": "m", "mass": "kg", "angle": "rad"})

    def test_full_mode_validates_with_joints_and_inertia(self) -> None:
        bodies = _body_manifest(self.tmp)
        model = build_sim_model(_mechanism(), bodies)
        manifest = build_manifest(
            name=model.name,
            output_dir=str(self.tmp),
            body_manifest=bodies,
            sim_model=model,
        )
        jsonschema.validate(manifest, self.schema)

        self.assertEqual(manifest["mode"], "full")
        self.assertEqual([j["name"] for j in manifest["joints"]], ["arm_joint"])

        joint = manifest["joints"][0]
        self.assertEqual(joint["type"], "revolute")
        self.assertEqual(joint["parent"], "base")
        self.assertEqual(joint["child"], "arm")
        self.assertAlmostEqual(joint["origin_xyz_m"][2], 0.1)  # 100 mm -> 0.1 m
        self.assertAlmostEqual(joint["limits"]["upper"], 1.5707963, places=5)

        base = next(lk for lk in manifest["links"] if lk["name"] == "base")
        self.assertTrue(base["is_root"])
        self.assertGreater(base["mass_kg"], 0.0)
        self.assertIn("inertia", base)
        self.assertEqual(base["mesh"]["frame"], "link_local")

    def test_full_mode_world_pose_follows_joint_tree(self) -> None:
        bodies = _body_manifest(self.tmp)
        model = build_sim_model(_mechanism(), bodies)
        manifest = build_manifest(
            name=model.name,
            output_dir=str(self.tmp),
            body_manifest=bodies,
            sim_model=model,
        )
        arm = next(lk for lk in manifest["links"] if lk["name"] == "arm")
        self.assertAlmostEqual(arm["world_pose"]["xyz_m"][2], 0.1)

    def test_link_names_match_joint_endpoints(self) -> None:
        bodies = _body_manifest(self.tmp)
        model = build_sim_model(_mechanism(), bodies)
        manifest = build_manifest(
            name=model.name,
            output_dir=str(self.tmp),
            body_manifest=bodies,
            sim_model=model,
        )
        names = {lk["name"] for lk in manifest["links"]}
        for joint in manifest["joints"]:
            self.assertIn(joint["parent"], names)
            self.assertIn(joint["child"], names)

    def test_rotor_actuators_from_drone_config(self) -> None:
        bodies = _body_manifest(self.tmp)
        model = build_sim_model(_mechanism(), bodies)
        drone_config = {
            "rotors": [
                {
                    "index": 0,
                    "joint": "arm_joint",
                    "direction": "cw",
                    "motor_constant": 1e-05,
                    "max_rot_velocity": 1000.0,
                    "moment_constant": 0.02,
                }
            ],
            "sensors": {"imu": True, "gps": False, "barometer": False, "magnetometer": False},
        }
        manifest = build_manifest(
            name=model.name,
            output_dir=str(self.tmp),
            body_manifest=bodies,
            sim_model=model,
            drone_config=drone_config,
        )
        jsonschema.validate(manifest, self.schema)

        actuator = manifest["actuators"][0]
        self.assertEqual(actuator["type"], "rotor")
        self.assertEqual(actuator["joint"], "arm_joint")
        self.assertEqual(actuator["link"], "arm")  # resolved through the joint
        self.assertEqual(actuator["direction"], "cw")
        # k * omega^2 = 1e-05 * 1000^2 = 10 N
        self.assertAlmostEqual(actuator["max_thrust_N"], 10.0)
        self.assertAlmostEqual(actuator["moment_constant"], 0.02)
        self.assertAlmostEqual(actuator["position_m"][2], 0.1)

        self.assertEqual(manifest["sensors"], [{"type": "imu", "link": "base"}])

    def test_no_drone_config_means_no_actuators(self) -> None:
        bodies = _body_manifest(self.tmp)
        model = build_sim_model(_mechanism(), bodies)
        manifest = build_manifest(
            name=model.name,
            output_dir=str(self.tmp),
            body_manifest=bodies,
            sim_model=model,
        )
        self.assertNotIn("actuators", manifest)
        self.assertNotIn("sensors", manifest)

    def test_write_manifest_round_trips(self) -> None:
        manifest = build_manifest(
            name="bodies_only",
            output_dir=str(self.tmp),
            body_manifest=_body_manifest(self.tmp),
        )
        path = write_manifest(manifest, str(self.tmp))
        self.assertEqual(Path(path).name, "manifest.json")
        with open(path) as fh:
            loaded = json.load(fh)
        jsonschema.validate(loaded, self.schema)
        self.assertEqual(loaded, manifest)


class TestExportSimPackageWritesManifest(unittest.TestCase):
    """cad.export_sim_package lands manifest.json on disk in both modes."""

    def setUp(self) -> None:
        self.schema = _load_schema("sim_package.schema.json")
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _client(self) -> Any:
        client = MagicMock()
        client.send_command.return_value = {
            "output_dir": str(self.tmp),
            "format": "stl",
            "body_count": 2,
            "bodies": _body_manifest(self.tmp),
        }
        return client

    @patch("server.tools_cad.get_client")
    def test_reduced_export_writes_manifest(self, mock_get: MagicMock) -> None:
        mock_get.return_value = self._client()
        result = cad_export_sim_package(output_dir=str(self.tmp))

        self.assertTrue(result["ok"], result)
        manifest_path = result["manifest_path"]
        self.assertEqual(Path(manifest_path).name, "manifest.json")
        with open(manifest_path) as fh:
            manifest = json.load(fh)
        jsonschema.validate(manifest, self.schema)
        self.assertEqual(manifest["mode"], "reduced")
        self.assertEqual(manifest["joints"], [])

    @patch("server.motion_store.get")
    @patch("server.tools_cad.get_client")
    def test_full_export_writes_manifest(
        self, mock_get: MagicMock, mock_mech_get: MagicMock
    ) -> None:
        mock_get.return_value = self._client()
        mock_mech_get.return_value = _mechanism()

        result = cad_export_sim_package(mechanism_id="mech_x", output_dir=str(self.tmp))

        self.assertTrue(result["ok"], result)
        with open(result["manifest_path"]) as fh:
            manifest = json.load(fh)
        jsonschema.validate(manifest, self.schema)
        self.assertEqual(manifest["mode"], "full")
        self.assertEqual(manifest["name"], "test_arm")
        self.assertEqual([j["name"] for j in manifest["joints"]], ["arm_joint"])
        # The manifest sits beside the meshes and the URDF it describes.
        self.assertEqual(Path(result["urdf_path"]).parent, Path(result["manifest_path"]).parent)


if __name__ == "__main__":
    unittest.main()
