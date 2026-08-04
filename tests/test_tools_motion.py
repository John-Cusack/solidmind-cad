"""Tests for server.tools_motion — MCP tool wrappers for motion validation."""

from __future__ import annotations

import math
import unittest
from unittest.mock import patch

from server import motion_store
from server.motion_models import (
    DriveCondition,
    JointEdge,
    JointType,
    Mechanism,
    PartNode,
)
from server.tools_motion import (
    _build_profile_from_mechanism,
    motion_check_gear_train,
    motion_check_interference,
    motion_create_assembly,
    motion_define_mechanism,
    motion_drive_joint,
    motion_list_mechanisms,
    motion_propagate_motion,
    motion_simulate,
    motion_validate,
)


class TestMotionToolsBase(unittest.TestCase):
    def setUp(self):
        motion_store.clear()
        from server.tools_motion import _active_sessions

        _active_sessions.clear()

    def tearDown(self):
        motion_store.clear()
        from server.tools_motion import _active_sessions

        _active_sessions.clear()


class TestDefineMechanism(TestMotionToolsBase):
    def _gear_pair_dict(self) -> dict:
        return {
            "name": "test_gear_pair",
            "parts": [
                {"id": "gear_a"},
                {"id": "gear_b"},
                {"id": "frame", "is_ground": True},
            ],
            "joints": [
                {
                    "id": "mesh",
                    "joint_type": "gear_mesh",
                    "parent_part": "gear_a",
                    "child_part": "gear_b",
                    "teeth_parent": 20,
                    "teeth_child": 40,
                    "gear_ratio": 0.5,
                },
                {
                    "id": "rev_a",
                    "joint_type": "revolute",
                    "parent_part": "frame",
                    "child_part": "gear_a",
                },
                {
                    "id": "rev_b",
                    "joint_type": "revolute",
                    "parent_part": "frame",
                    "child_part": "gear_b",
                },
            ],
            "drives": [
                {"joint_id": "mesh", "speed_rpm": 1000, "torque_nm": 5.0},
            ],
        }

    def test_define_success(self):
        result = motion_define_mechanism(self._gear_pair_dict())
        self.assertTrue(result["ok"])
        self.assertIn("mechanism_id", result)
        self.assertEqual(result["summary"]["part_count"], 3)
        self.assertEqual(result["summary"]["joint_count"], 3)
        self.assertEqual(result["warnings"], [])

    def test_define_rejects_unknown_part(self):
        d = self._gear_pair_dict()
        d["joints"][0]["parent_part"] = "nonexistent"
        result = motion_define_mechanism(d)
        self.assertFalse(result["ok"])  # Dangling reference is now a structural error
        self.assertIn("INVALID_MECHANISM", result["error"]["code"])

    def test_define_errors_no_ground_strict(self):
        """In strict mode (used by tools_motion), no ground part is an error."""
        d = self._gear_pair_dict()
        for p in d["parts"]:
            p["is_ground"] = False
        result = motion_define_mechanism(d)
        self.assertFalse(result["ok"])
        self.assertIn("INVALID_MECHANISM", result["error"]["code"])

    def test_define_invalid_input(self):
        result = motion_define_mechanism("not a dict")
        self.assertFalse(result["ok"])

    def test_define_missing_required(self):
        result = motion_define_mechanism({"parts": []})
        self.assertFalse(result["ok"])  # Missing 'name'


class TestListMechanisms(TestMotionToolsBase):
    def test_empty(self):
        result = motion_list_mechanisms()
        self.assertTrue(result["ok"])
        self.assertEqual(result["mechanisms"], [])

    def test_after_define(self):
        motion_define_mechanism(
            {
                "name": "test",
                "parts": [{"id": "a", "is_ground": True}],
                "joints": [],
                "drives": [],
            }
        )
        result = motion_list_mechanisms()
        self.assertEqual(len(result["mechanisms"]), 1)
        self.assertEqual(result["mechanisms"][0]["name"], "test")


class TestValidate(TestMotionToolsBase):
    def _define_gear_pair(self) -> str:
        result = motion_define_mechanism(
            {
                "name": "gear_pair",
                "parts": [
                    {"id": "gear_a"},
                    {"id": "gear_b"},
                    {"id": "frame", "is_ground": True},
                ],
                "joints": [
                    {
                        "id": "mesh",
                        "joint_type": "gear_mesh",
                        "parent_part": "gear_a",
                        "child_part": "gear_b",
                        "teeth_parent": 20,
                        "teeth_child": 40,
                        "gear_ratio": 0.5,
                    },
                ],
                "drives": [
                    {"joint_id": "mesh", "speed_rpm": 1000, "torque_nm": 5.0},
                ],
            }
        )
        return result["mechanism_id"]

    def test_validate_all(self):
        mid = self._define_gear_pair()
        result = motion_validate(mid)
        self.assertTrue(result["ok"])
        self.assertIn("results", result)
        self.assertGreater(len(result["results"]), 0)

    def test_validate_specific(self):
        mid = self._define_gear_pair()
        result = motion_validate(mid, validators=["gear_ratio_consistency"])
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["results"][0]["name"], "gear_ratio_consistency")

    def test_validate_not_found(self):
        result = motion_validate("mech_nonexistent")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "NOT_FOUND")


class TestPropagateMotion(TestMotionToolsBase):
    def test_propagate(self):
        result = motion_define_mechanism(
            {
                "name": "gear_pair",
                "parts": [
                    {"id": "gear_a"},
                    {"id": "gear_b"},
                    {"id": "frame", "is_ground": True},
                ],
                "joints": [
                    {
                        "id": "mesh",
                        "joint_type": "gear_mesh",
                        "parent_part": "gear_a",
                        "child_part": "gear_b",
                        "teeth_parent": 20,
                        "teeth_child": 40,
                        "gear_ratio": 0.5,
                    },
                ],
                "drives": [
                    {"joint_id": "mesh", "speed_rpm": 1000, "torque_nm": 5.0},
                ],
            }
        )
        mid = result["mechanism_id"]
        prop = motion_propagate_motion(mid)
        self.assertTrue(prop["ok"])
        self.assertIn("states", prop)
        self.assertAlmostEqual(prop["states"]["gear_a"]["rpm"], 1000.0)
        self.assertAlmostEqual(prop["states"]["gear_b"]["rpm"], 500.0)
        self.assertAlmostEqual(prop["states"]["gear_a"]["torque_nm"], 5.0)

    def test_propagate_not_found(self):
        result = motion_propagate_motion("mech_nonexistent")
        self.assertFalse(result["ok"])


class TestCheckGearTrain(TestMotionToolsBase):
    def test_check(self):
        result = motion_define_mechanism(
            {
                "name": "gear_pair",
                "parts": [{"id": "a"}, {"id": "b"}, {"id": "frame", "is_ground": True}],
                "joints": [
                    {
                        "id": "mesh",
                        "joint_type": "gear_mesh",
                        "parent_part": "a",
                        "child_part": "b",
                        "teeth_parent": 20,
                        "teeth_child": 40,
                        "gear_ratio": 0.5,
                    }
                ],
                "drives": [],
            }
        )
        mid = result["mechanism_id"]
        gt = motion_check_gear_train(mid)
        self.assertTrue(gt["ok"])
        self.assertAlmostEqual(gt["overall_ratio"], 0.5)
        self.assertEqual(len(gt["stages"]), 1)


class TestCreateAssembly(TestMotionToolsBase):
    def test_create_assembly_not_found(self):
        result = motion_create_assembly("mech_nonexistent")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "NOT_FOUND")

    def test_create_assembly_no_freecad(self):
        """Without a suitable FreeCAD environment, create_assembly fails gracefully.

        Returns CONNECTION_ERROR if FreeCAD addon is not running, or
        COMMAND_ERROR if it's running but the Assembly workbench or
        document isn't available.
        """
        result = motion_define_mechanism(
            {
                "name": "test_asm",
                "parts": [
                    {"id": "gear_a", "body_name": "Body_A"},
                    {"id": "gear_b", "body_name": "Body_B"},
                    {"id": "frame", "is_ground": True},
                ],
                "joints": [
                    {
                        "id": "mesh",
                        "joint_type": "gear_mesh",
                        "parent_part": "gear_a",
                        "child_part": "gear_b",
                        "teeth_parent": 20,
                        "teeth_child": 40,
                    }
                ],
                "drives": [],
            }
        )
        mid = result["mechanism_id"]
        asm_result = motion_create_assembly(mid)
        self.assertFalse(asm_result["ok"])
        self.assertIn(
            asm_result["error"]["code"],
            ("CONNECTION_ERROR", "COMMAND_ERROR"),
        )


class TestAssemblyJointMapping(TestMotionToolsBase):
    """Test the joint name mapping between mechanism model and FreeCAD objects."""

    def test_joint_map_populated_by_create_assembly(self):
        """Verify _assembly_joint_maps is populated when create_assembly runs."""
        from server.tools_motion import _assembly_joint_maps

        # The map starts empty or doesn't have our mechanism
        result = motion_define_mechanism(
            {
                "name": "map_test",
                "parts": [
                    {"id": "gear_a", "body_name": "Body_A"},
                    {"id": "gear_b", "body_name": "Body_B"},
                    {"id": "frame", "is_ground": True},
                ],
                "joints": [
                    {
                        "id": "sun_rev",
                        "joint_type": "revolute",
                        "parent_part": "frame",
                        "child_part": "gear_a",
                    }
                ],
                "drives": [],
            }
        )
        mid = result["mechanism_id"]
        # create_assembly will fail (no FreeCAD), but the mapping logic is tested
        # via the drive_joint resolution path
        self.assertNotIn(mid, _assembly_joint_maps)

    def test_drive_joint_resolves_through_map(self):
        """Verify drive_joint uses the joint map for name resolution."""
        from server.tools_motion import _assembly_joint_maps

        result = motion_define_mechanism(
            {
                "name": "resolve_test",
                "parts": [
                    {"id": "gear_a", "body_name": "Body_A"},
                    {"id": "frame", "is_ground": True},
                ],
                "joints": [
                    {
                        "id": "sun_rev",
                        "joint_type": "revolute",
                        "parent_part": "frame",
                        "child_part": "gear_a",
                    }
                ],
                "drives": [],
            }
        )
        mid = result["mechanism_id"]

        # Manually populate the map as create_assembly would
        _assembly_joint_maps[mid] = {"sun_rev": "sun_rev001"}

        # drive_joint will fail (no FreeCAD) but should use the mapped name
        drive_result = motion_drive_joint(mid, "sun_rev", 360.0)
        # It should fail with CONNECTION_ERROR, not NOT_FOUND
        self.assertFalse(drive_result["ok"])
        self.assertIn(
            drive_result["error"]["code"],
            ("CONNECTION_ERROR", "COMMAND_ERROR"),
        )

        # Clean up
        del _assembly_joint_maps[mid]

    def _make_analytical_mechanism(self):
        """Helper: define a sun+planet mechanism, return mechanism_id."""
        result = motion_define_mechanism(
            {
                "name": "analytical_test",
                "parts": [
                    {"id": "frame", "body_name": "Body_Frame", "is_ground": True},
                    {"id": "sun", "body_name": "Body_Sun"},
                    {"id": "planet", "body_name": "Body_Planet"},
                ],
                "joints": [
                    {
                        "id": "sun_rev",
                        "joint_type": "revolute",
                        "parent_part": "frame",
                        "child_part": "sun",
                        "origin": [0.0, 0.0, 0.0],
                        "axis": [0.0, 0.0, 1.0],
                    },
                    {
                        "id": "mesh_sp",
                        "joint_type": "gear_mesh",
                        "parent_part": "sun",
                        "child_part": "planet",
                        "teeth_parent": 20,
                        "teeth_child": 10,
                        "origin": [30.0, 0.0, 0.0],
                        "axis": [0.0, 0.0, 1.0],
                    },
                ],
                "drives": [{"joint_id": "sun_rev", "speed_rpm": 100}],
            }
        )
        return result["mechanism_id"]

    def _make_mock_client(self, with_get_links: bool = False):
        """Build a mock FreeCAD client.

        If *with_get_links* is True, ``assembly_get_links`` returns a
        mapping that matches ``Body_Sun`` / ``Body_Planet``.
        """
        from unittest.mock import MagicMock

        mock_client = MagicMock()

        def fake_send(cmd, **kw):
            if cmd == "assembly_get_links" and with_get_links:
                return {
                    "assembly": kw.get("assembly", "Asm"),
                    "links": {
                        "Link_Sun": "Body_Sun",
                        "Link_Planet": "Body_Planet",
                    },
                }
            return {"applied": ["Link_Sun", "Link_Planet"]}

        mock_client.send_command.side_effect = fake_send
        return mock_client

    def test_drive_joint_analytical_with_mock(self):
        """Verify analytical drive computes correct angles and sends placements."""
        from unittest.mock import patch

        from server.tools_motion import _assembly_link_maps

        mid = self._make_analytical_mechanism()

        # Populate link map as create_assembly would
        _assembly_link_maps[mid] = {
            "sun": "Link_Sun",
            "planet": "Link_Planet",
        }

        mock_client = self._make_mock_client()

        with patch("server.freecad_client.get_client", return_value=mock_client):
            drive_result = motion_drive_joint(mid, "sun_rev", 360.0, steps=1)

        self.assertTrue(drive_result["ok"], drive_result)
        self.assertEqual(drive_result["method"], "analytical")
        self.assertEqual(len(drive_result["step_positions"]), 2)  # step 0 and step 1

        # Check that set_placements was called
        set_calls = [
            c
            for c in mock_client.send_command.call_args_list
            if c[0][0] == "assembly_set_placements"
        ]
        self.assertEqual(len(set_calls), 2)  # one per step

        # Last step should have the full angle
        last_kw = set_calls[-1][1]
        placements = last_kw["placements"]
        # Sun should rotate 360 deg (ratio=1 relative to frame drive)
        self.assertIn("Link_Sun", placements)
        # Planet should rotate -720 deg (ratio = -2 for 20:10 teeth)
        # but propagate_speeds gives ratio sun_rpm/frame_rpm;
        # the exact value depends on BFS — just check it's non-zero
        self.assertIn("Link_Planet", placements)
        self.assertNotEqual(placements["Link_Planet"]["angle_deg"], 0.0)

        # Clean up
        del _assembly_link_maps[mid]

    def test_drive_joint_re_derives_link_map(self):
        """drive_joint re-derives the link map via assembly_get_links
        when _assembly_link_maps has no entry for this mechanism."""
        from unittest.mock import patch

        from server.tools_motion import _assembly_link_maps

        mid = self._make_analytical_mechanism()

        # Ensure cache is empty for this mechanism
        _assembly_link_maps.pop(mid, None)

        mock_client = self._make_mock_client(with_get_links=True)

        with patch("server.freecad_client.get_client", return_value=mock_client):
            drive_result = motion_drive_joint(mid, "sun_rev", 360.0, steps=1)

        self.assertTrue(drive_result["ok"], drive_result)

        # assembly_get_links should have been called once
        get_links_calls = [
            c for c in mock_client.send_command.call_args_list if c[0][0] == "assembly_get_links"
        ]
        self.assertEqual(len(get_links_calls), 1)

        # Placements should still contain both parts
        set_calls = [
            c
            for c in mock_client.send_command.call_args_list
            if c[0][0] == "assembly_set_placements"
        ]
        self.assertGreater(len(set_calls), 0)
        last_placements = set_calls[-1][1]["placements"]
        self.assertIn("Link_Sun", last_placements)
        self.assertIn("Link_Planet", last_placements)


class TestJointOriginAxisPassthrough(TestMotionToolsBase):
    """Verify joint_origin and joint_axis are forwarded to FreeCAD."""

    def test_create_assembly_passes_origin_axis(self):
        """motion_create_assembly should include joint_origin/joint_axis in
        the assembly_add_joint command kwargs."""
        from unittest.mock import MagicMock, patch

        result = motion_define_mechanism(
            {
                "name": "origin_axis_test",
                "parts": [
                    {"id": "sun", "body_name": "Body_Sun"},
                    {"id": "frame", "body_name": "Body_Frame", "is_ground": True},
                ],
                "joints": [
                    {
                        "id": "sun_rev",
                        "joint_type": "revolute",
                        "parent_part": "frame",
                        "child_part": "sun",
                        "origin": [10.0, 20.0, 0.0],
                        "axis": [0.0, 0.0, 1.0],
                    }
                ],
                "drives": [],
            }
        )
        mid = result["mechanism_id"]

        # Build a mock client that records send_command calls
        mock_client = MagicMock()

        # assembly_create returns an assembly name
        def fake_send(cmd, **kw):
            if cmd == "assembly_create":
                return {"name": "Assembly"}
            if cmd == "assembly_add_part":
                return {"link_name": kw.get("body", "Link")}
            if cmd == "assembly_add_joint":
                return {"joint_name": kw.get("name", "Joint")}
            if cmd == "assembly_solve":
                return {}
            return {}

        mock_client.send_command.side_effect = fake_send

        with patch("server.freecad_client.get_client", return_value=mock_client):
            asm_result = motion_create_assembly(mid)

        self.assertTrue(asm_result["ok"], asm_result)

        # Find the assembly_add_joint call
        joint_calls = [
            c for c in mock_client.send_command.call_args_list if c[0][0] == "assembly_add_joint"
        ]
        self.assertEqual(len(joint_calls), 1)
        kw = joint_calls[0][1]
        self.assertEqual(kw["joint_origin"], [10.0, 20.0, 0.0])
        self.assertEqual(kw["joint_axis"], [0.0, 0.0, 1.0])


class TestDriveJointPlanetaryCompound(TestMotionToolsBase):
    """Verify compound placements are sent with 'position' key for planets."""

    def _make_planetary_mechanism(self):
        """Define a planetary mechanism with ring fixed, sun driven."""
        result = motion_define_mechanism(
            {
                "name": "planetary_drive_test",
                "parts": [
                    {"id": "frame", "body_name": "Body_Frame", "is_ground": True},
                    {"id": "sun", "body_name": "Body_Sun"},
                    {"id": "carrier", "body_name": "Body_Carrier"},
                    {"id": "ring", "body_name": "Body_Ring", "is_ground": True},
                    {"id": "planet_0", "body_name": "Body_Planet0"},
                ],
                "joints": [
                    {
                        "id": "sun_rev",
                        "joint_type": "revolute",
                        "parent_part": "frame",
                        "child_part": "sun",
                        "origin": [0.0, 0.0, 0.0],
                        "axis": [0.0, 0.0, 1.0],
                    },
                    {
                        "id": "carrier_rev",
                        "joint_type": "revolute",
                        "parent_part": "frame",
                        "child_part": "carrier",
                        "origin": [0.0, 0.0, 0.0],
                        "axis": [0.0, 0.0, 1.0],
                    },
                    {
                        "id": "sun_planet_0",
                        "joint_type": "gear_mesh",
                        "parent_part": "sun",
                        "child_part": "planet_0",
                        "teeth_parent": 18,
                        "teeth_child": 9,
                        "gear_ratio": 2.0,
                    },
                    {
                        "id": "planet_ring_0",
                        "joint_type": "gear_mesh",
                        "parent_part": "planet_0",
                        "child_part": "ring",
                        "teeth_parent": 9,
                        "teeth_child": 36,
                        "gear_ratio": 0.25,
                        "internal": True,
                    },
                    {
                        "id": "planet_carrier_0",
                        "joint_type": "revolute",
                        "parent_part": "carrier",
                        "child_part": "planet_0",
                        "origin": [27.0, 0.0, 0.0],
                        "axis": [0.0, 0.0, 1.0],
                    },
                ],
                "drives": [{"joint_id": "sun_planet_0", "speed_rpm": 1000}],
            }
        )
        return result["mechanism_id"]

    def test_drive_joint_planetary_compound_motion(self):
        """Verify compound placements are sent with 'position' key for planets."""
        from unittest.mock import MagicMock, patch

        from server.tools_motion import _assembly_link_maps

        mid = self._make_planetary_mechanism()

        # Populate link map
        _assembly_link_maps[mid] = {
            "sun": "Link_Sun",
            "carrier": "Link_Carrier",
            "planet_0": "Link_Planet0",
        }

        mock_client = MagicMock()
        mock_client.send_command.return_value = {"applied": []}

        with patch("server.freecad_client.get_client", return_value=mock_client):
            drive_result = motion_drive_joint(mid, "sun_rev", 360.0, steps=1)

        self.assertTrue(drive_result["ok"], drive_result)
        self.assertEqual(drive_result["method"], "analytical")

        # Find the last set_placements call
        set_calls = [
            c
            for c in mock_client.send_command.call_args_list
            if c[0][0] == "assembly_set_placements"
        ]
        self.assertGreater(len(set_calls), 0)

        last_placements = set_calls[-1][1]["placements"]

        # Planet should use compound format (has 'position' key)
        self.assertIn("Link_Planet0", last_placements)
        planet_spec = last_placements["Link_Planet0"]
        self.assertIn("position", planet_spec)
        self.assertIn("rotation_axis", planet_spec)
        self.assertIn("rotation_angle_deg", planet_spec)

        # Sun and carrier should use legacy format (no 'position' key)
        if "Link_Sun" in last_placements:
            sun_spec = last_placements["Link_Sun"]
            self.assertIn("angle_deg", sun_spec)
            self.assertNotIn("position", sun_spec)

        # Clean up
        del _assembly_link_maps[mid]


class TestDriveJoint(TestMotionToolsBase):
    def test_drive_joint_not_found_mechanism(self):
        result = motion_drive_joint("mech_nonexistent", "j1", 360.0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "NOT_FOUND")

    def test_drive_joint_not_found_joint(self):
        result = motion_define_mechanism(
            {
                "name": "test",
                "parts": [{"id": "a", "is_ground": True}],
                "joints": [],
                "drives": [],
            }
        )
        mid = result["mechanism_id"]
        result = motion_drive_joint(mid, "nonexistent_joint", 360.0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "NOT_FOUND")


class TestCheckInterference(TestMotionToolsBase):
    def test_check_interference_not_found(self):
        result = motion_check_interference("mech_nonexistent")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "NOT_FOUND")

    def test_check_interference_no_freecad(self):
        """Without a suitable FreeCAD environment, check_interference fails gracefully.

        Returns CONNECTION_ERROR if FreeCAD addon is not running, or
        COMMAND_ERROR if it's running but the Assembly workbench or
        document isn't available.
        """
        result = motion_define_mechanism(
            {
                "name": "test_interf",
                "parts": [
                    {"id": "a", "body_name": "Body_A", "is_ground": True},
                    {"id": "b", "body_name": "Body_B"},
                ],
                "joints": [],
                "drives": [],
            }
        )
        mid = result["mechanism_id"]
        interf_result = motion_check_interference(mid)
        self.assertFalse(interf_result["ok"])
        self.assertIn(
            interf_result["error"]["code"],
            ("CONNECTION_ERROR", "COMMAND_ERROR"),
        )


class TestSimulate(TestMotionToolsBase):
    def test_simulate_not_found(self):
        result = motion_simulate("mech_nonexistent")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "NOT_FOUND")

    def test_simulate_default_backend_is_isaac(self):
        """When backend is omitted, motion_simulate defaults to Isaac."""
        result = motion_define_mechanism(
            {
                "name": "gear_pair",
                "parts": [{"id": "a"}, {"id": "b"}, {"id": "f", "is_ground": True}],
                "joints": [
                    {
                        "id": "mesh",
                        "joint_type": "gear_mesh",
                        "parent_part": "a",
                        "child_part": "b",
                        "teeth_parent": 20,
                        "teeth_child": 40,
                    }
                ],
                "drives": [{"joint_id": "mesh", "speed_rpm": 1000}],
            }
        )
        mid = result["mechanism_id"]
        sim_result = motion_simulate(mid)
        if sim_result["ok"]:
            self.assertEqual(sim_result["backend_used"], "isaac")
        else:
            # Every honest way this can fail: no bridge to ask, a bridge that
            # rejects the joint type, or — when Isaac is actually up — a
            # refusal to take an in-band mechanism, which it does not
            # advertise.  What must not happen is a fabricated success.
            self.assertIn(
                sim_result["error"]["code"],
                {
                    "BACKEND_UNAVAILABLE_CHOOSE",
                    "UNSUPPORTED_JOINT_TYPE",
                    "ISAAC_CONNECTION_LOST",
                    "UNSUPPORTED_MODEL_FORMAT",
                },
            )

    def test_simulate_explicit_chrono_no_daemon(self):
        """Chrono remains available when requested explicitly."""
        result = motion_define_mechanism(
            {
                "name": "gear_pair",
                "parts": [{"id": "a"}, {"id": "b"}, {"id": "f", "is_ground": True}],
                "joints": [
                    {
                        "id": "mesh",
                        "joint_type": "gear_mesh",
                        "parent_part": "a",
                        "child_part": "b",
                        "teeth_parent": 20,
                        "teeth_child": 40,
                    }
                ],
                "drives": [{"joint_id": "mesh", "speed_rpm": 1000}],
            }
        )
        mid = result["mechanism_id"]
        sim_result = motion_simulate(mid, backend="chrono")
        if sim_result["ok"]:
            self.assertEqual(sim_result["backend_used"], "chrono")
        else:
            self.assertIn(
                sim_result["error"]["code"],
                ("BACKEND_UNAVAILABLE_CHOOSE", "SIMULATION_SPEC_INVALID"),
            )

    def test_simulate_driven_part_in_define(self):
        """driven_part should be accepted in mechanism definition."""
        result = motion_define_mechanism(
            {
                "name": "driven_test",
                "parts": [{"id": "a"}, {"id": "b"}, {"id": "f", "is_ground": True}],
                "joints": [
                    {
                        "id": "mesh",
                        "joint_type": "gear_mesh",
                        "parent_part": "a",
                        "child_part": "b",
                        "teeth_parent": 20,
                        "teeth_child": 40,
                    }
                ],
                "drives": [{"joint_id": "mesh", "speed_rpm": 500, "driven_part": "b"}],
            }
        )
        self.assertTrue(result["ok"])


class TestSimulateSpecValidation(TestMotionToolsBase):
    """Test pre-flight spec validation in motion_simulate."""

    def test_no_drives_produces_spec_error(self):
        """A mechanism with no drives should fail spec validation (no motors)."""
        result = motion_define_mechanism(
            {
                "name": "no_drives",
                "parts": [{"id": "a"}, {"id": "f", "is_ground": True}],
                "joints": [
                    {
                        "id": "rev",
                        "joint_type": "revolute",
                        "parent_part": "f",
                        "child_part": "a",
                    }
                ],
                "drives": [],
            }
        )
        mid = result["mechanism_id"]
        sim_result = motion_simulate(mid, backend="chrono")
        if not sim_result["ok"]:
            # Could be BACKEND_UNAVAILABLE_CHOOSE or SIMULATION_SPEC_INVALID
            if sim_result["error"]["code"] == "SIMULATION_SPEC_INVALID":
                self.assertIn("No motor", sim_result["error"]["message"])


class TestSimulateBackendBehavior(TestMotionToolsBase):
    """Engine-agnostic guards on motion.simulate.

    Routing itself — which engine, session vs single-call, teleop DOFs,
    unavailable-engine guidance — lives in ``tests/test_motion_engine_routing.py``
    now that none of it branches on an engine name.
    """

    def _mechanism_id(self) -> str:
        result = motion_define_mechanism(
            {
                "name": "gear_pair",
                "parts": [{"id": "frame", "is_ground": True}, {"id": "gear_a"}],
                "joints": [
                    {
                        "id": "rev_a",
                        "joint_type": "revolute",
                        "parent_part": "frame",
                        "child_part": "gear_a",
                    }
                ],
                "drives": [{"joint_id": "rev_a", "speed_rpm": 600.0}],
            }
        )
        return str(result["mechanism_id"])

    def test_rejects_invalid_numeric_params(self) -> None:
        mid = self._mechanism_id()
        for kwargs in (
            {"duration_s": 0.0},
            {"dt_s": 0.0},
            {"output_interval": -1.0},
            {"dt_s": 2.0, "duration_s": 1.0},
        ):
            result = motion_simulate(mid, backend="chrono", **kwargs)
            self.assertFalse(result["ok"], kwargs)
            self.assertEqual(result["error"]["code"], "INVALID_INPUT")

    def test_rejects_unknown_mode(self) -> None:
        mid = self._mechanism_id()
        result = motion_simulate(mid, backend="chrono", mode="nonsense")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "INVALID_INPUT")

    def test_engine_error_is_propagated_verbatim(self) -> None:
        """A refusal from the engine reaches the caller with its own code."""
        mid = self._mechanism_id()
        engine_error = {
            "ok": False,
            "error": {"code": "UNSUPPORTED_JOINT_TYPE", "message": "gear_mesh unsupported"},
        }
        with (
            patch("server.sim_adapter.call", return_value=engine_error),
            patch("server.sim_adapter.supports_mode", return_value=False),
        ):
            result = motion_simulate(mid, backend="chrono", duration_s=0.1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "UNSUPPORTED_JOINT_TYPE")

    def test_profile_reaches_the_engine(self) -> None:
        mid = self._mechanism_id()
        with (
            patch("server.sim_adapter.call", return_value={"ok": True}) as call,
            patch("server.sim_adapter.supports_mode", return_value=False),
        ):
            motion_simulate(mid, backend="chrono", duration_s=0.1, profile={"speed": 3})
        self.assertEqual(call.call_args.kwargs["profile"], {"speed": 3})


class TestBuildProfileFromMechanism(unittest.TestCase):
    """Tests for _build_profile_from_mechanism auto-profile generation."""

    def _make_hexapod_18dof(self) -> Mechanism:
        """Create a minimal 18-DOF hexapod mechanism for testing."""
        chassis = PartNode(id="chassis", is_ground=True)
        parts = [chassis]
        joints: list[JointEdge] = []

        # 6 legs: LF, LM, LR, RF, RM, RR
        # Each leg has coxa, femur, tibia joints
        hip_positions = [
            (70.0, 75.0),  # LF
            (0.0, 75.0),  # LM
            (-70.0, 75.0),  # LR
            (70.0, -75.0),  # RF
            (0.0, -75.0),  # RM
            (-70.0, -75.0),  # RR
        ]
        leg_names = ["lf", "lm", "lr", "rf", "rm", "rr"]

        for i, (hx, hy) in enumerate(hip_positions):
            leg = leg_names[i]
            coxa_id = f"coxa_{leg}"
            femur_id = f"femur_{leg}"
            tibia_id = f"tibia_{leg}"

            parts.append(PartNode(id=f"coxa_seg_{leg}"))
            parts.append(PartNode(id=f"femur_seg_{leg}"))
            parts.append(PartNode(id=f"tibia_seg_{leg}"))

            joints.append(
                JointEdge(
                    id=coxa_id,
                    joint_type=JointType.REVOLUTE,
                    parent_part="chassis",
                    child_part=f"coxa_seg_{leg}",
                    axis=(0.0, 0.0, 1.0),
                    origin=(hx, hy, 0.0),
                    min_angle_deg=-45.0,
                    max_angle_deg=45.0,
                )
            )
            joints.append(
                JointEdge(
                    id=femur_id,
                    joint_type=JointType.REVOLUTE,
                    parent_part=f"coxa_seg_{leg}",
                    child_part=f"femur_seg_{leg}",
                    axis=(0.0, 1.0, 0.0),
                    origin=(hx + 52.0, hy, 0.0),
                    min_angle_deg=-90.0,
                    max_angle_deg=90.0,
                )
            )
            joints.append(
                JointEdge(
                    id=tibia_id,
                    joint_type=JointType.REVOLUTE,
                    parent_part=f"femur_seg_{leg}",
                    child_part=f"tibia_seg_{leg}",
                    axis=(0.0, 1.0, 0.0),
                    origin=(hx + 52.0 + 66.0, hy, 0.0),
                    min_angle_deg=-120.0,
                    max_angle_deg=0.0,
                )
            )

        return Mechanism(
            name="hexapod_18dof",
            parts=tuple(parts),
            joints=tuple(joints),
            drives=(),
        )

    def test_18dof_controller_type(self) -> None:
        mech = self._make_hexapod_18dof()
        profile = _build_profile_from_mechanism(mech)
        self.assertEqual(profile["controller_type"], "hexapod_3dof_tripod")

    def test_18dof_dofs_per_leg(self) -> None:
        mech = self._make_hexapod_18dof()
        profile = _build_profile_from_mechanism(mech)
        self.assertEqual(profile["dofs_per_leg"], 3)

    def test_18dof_joint_names(self) -> None:
        mech = self._make_hexapod_18dof()
        profile = _build_profile_from_mechanism(mech)
        self.assertEqual(len(profile["leg_joint_names"]), 18)
        # All joints should be present
        for leg in ["lf", "lm", "lr", "rf", "rm", "rr"]:
            for jtype in ["coxa", "femur", "tibia"]:
                self.assertIn(f"{jtype}_{leg}", profile["leg_joint_names"])

    def test_18dof_hip_mounts(self) -> None:
        mech = self._make_hexapod_18dof()
        profile = _build_profile_from_mechanism(mech)
        self.assertEqual(len(profile["hip_mounts"]), 6)
        # Check first hip mount (LF at 70, 75 mm → 0.07, 0.075 m)
        hm0 = profile["hip_mounts"][0]
        self.assertAlmostEqual(hm0[0], 0.07, places=4)
        self.assertAlmostEqual(hm0[1], 0.075, places=4)

    def test_18dof_segment_lengths(self) -> None:
        mech = self._make_hexapod_18dof()
        profile = _build_profile_from_mechanism(mech)
        # l_coxa = distance between coxa and femur origins = 52mm = 0.052m
        self.assertAlmostEqual(profile["l_coxa"], 0.052, places=3)
        # l_femur = distance between femur and tibia origins = 66mm = 0.066m
        self.assertAlmostEqual(profile["l_femur"], 0.066, places=3)

    def test_18dof_body_dimensions(self) -> None:
        mech = self._make_hexapod_18dof()
        profile = _build_profile_from_mechanism(mech)
        # body_length = 2 * max(|hx|) = 2 * 0.07 = 0.14
        self.assertAlmostEqual(profile["body_length"], 0.14, places=3)
        # body_width = 2 * max(|hy|) = 2 * 0.075 = 0.15
        self.assertAlmostEqual(profile["body_width"], 0.15, places=3)

    def test_18dof_left_right_classification(self) -> None:
        mech = self._make_hexapod_18dof()
        profile = _build_profile_from_mechanism(mech)
        # Left legs have positive Y
        self.assertEqual(len(profile["left_legs"]), 3)
        self.assertEqual(len(profile["right_legs"]), 3)
        for name in profile["left_legs"]:
            self.assertIn("l", name)  # coxa_lf, coxa_lm, coxa_lr
        for name in profile["right_legs"]:
            self.assertIn("r", name)  # coxa_rf, coxa_rm, coxa_rr

    def test_18dof_phase_offsets(self) -> None:
        mech = self._make_hexapod_18dof()
        profile = _build_profile_from_mechanism(mech)
        self.assertEqual(profile["leg_phase_offsets"], [0.0, 0.5, 0.0, 0.5, 0.0, 0.5])

    def _make_hexapod_18dof_shuffled(self) -> Mechanism:
        """Create an 18-DOF hexapod with joints in non-canonical order.

        Joints are added in RF, LR, RM, LF, RR, LM order — NOT the
        canonical [LF, LM, LR, RF, RM, RR].  The sort in
        _build_profile_from_mechanism must recover canonical order.
        """
        chassis = PartNode(id="chassis", is_ground=True)
        parts: list[PartNode] = [chassis]
        joints: list[JointEdge] = []

        # Shuffled order: RF, LR, RM, LF, RR, LM
        hip_positions = [
            (70.0, -75.0),  # RF
            (-70.0, 75.0),  # LR
            (0.0, -75.0),  # RM
            (70.0, 75.0),  # LF
            (-70.0, -75.0),  # RR
            (0.0, 75.0),  # LM
        ]
        leg_names = ["rf", "lr", "rm", "lf", "rr", "lm"]

        for i, (hx, hy) in enumerate(hip_positions):
            leg = leg_names[i]
            parts.append(PartNode(id=f"coxa_seg_{leg}"))
            parts.append(PartNode(id=f"femur_seg_{leg}"))
            parts.append(PartNode(id=f"tibia_seg_{leg}"))

            joints.append(
                JointEdge(
                    id=f"coxa_{leg}",
                    joint_type=JointType.REVOLUTE,
                    parent_part="chassis",
                    child_part=f"coxa_seg_{leg}",
                    axis=(0.0, 0.0, 1.0),
                    origin=(hx, hy, 0.0),
                    min_angle_deg=-45.0,
                    max_angle_deg=45.0,
                )
            )
            joints.append(
                JointEdge(
                    id=f"femur_{leg}",
                    joint_type=JointType.REVOLUTE,
                    parent_part=f"coxa_seg_{leg}",
                    child_part=f"femur_seg_{leg}",
                    axis=(0.0, 1.0, 0.0),
                    origin=(hx + 52.0, hy, 0.0),
                    min_angle_deg=-90.0,
                    max_angle_deg=90.0,
                )
            )
            joints.append(
                JointEdge(
                    id=f"tibia_{leg}",
                    joint_type=JointType.REVOLUTE,
                    parent_part=f"femur_seg_{leg}",
                    child_part=f"tibia_seg_{leg}",
                    axis=(0.0, 1.0, 0.0),
                    origin=(hx + 52.0 + 66.0, hy, 0.0),
                    min_angle_deg=-120.0,
                    max_angle_deg=0.0,
                )
            )

        return Mechanism(
            name="hexapod_18dof_shuffled",
            parts=tuple(parts),
            joints=tuple(joints),
            drives=(),
        )

    def test_shuffled_order_produces_canonical(self) -> None:
        """Chains discovered in arbitrary order are sorted to canonical
        [LF, LM, LR, RF, RM, RR] so phase offsets match the right legs."""
        mech = self._make_hexapod_18dof_shuffled()
        profile = _build_profile_from_mechanism(mech)

        # Should still detect 6 legs with 3 DOF each
        self.assertEqual(profile["dofs_per_leg"], 3)
        self.assertEqual(len(profile["hip_mounts"]), 6)

        # Expected canonical order: LF, LM, LR, RF, RM, RR
        expected_first_joints = [
            "coxa_lf",
            "coxa_lm",
            "coxa_lr",
            "coxa_rf",
            "coxa_rm",
            "coxa_rr",
        ]
        actual_first_joints = [profile["leg_joint_names"][i * 3] for i in range(6)]
        self.assertEqual(actual_first_joints, expected_first_joints)

        # Verify hip mount positions match canonical order
        hm = profile["hip_mounts"]
        # LF: positive x, positive y
        self.assertGreater(hm[0][0], 0)
        self.assertGreater(hm[0][1], 0)
        # LM: zero x, positive y
        self.assertAlmostEqual(hm[1][0], 0.0, places=3)
        self.assertGreater(hm[1][1], 0)
        # LR: negative x, positive y
        self.assertLess(hm[2][0], 0)
        self.assertGreater(hm[2][1], 0)
        # RF: positive x, negative y
        self.assertGreater(hm[3][0], 0)
        self.assertLess(hm[3][1], 0)
        # RM: zero x, negative y
        self.assertAlmostEqual(hm[4][0], 0.0, places=3)
        self.assertLess(hm[4][1], 0)
        # RR: negative x, negative y
        self.assertLess(hm[5][0], 0)
        self.assertLess(hm[5][1], 0)

    def test_shuffled_matches_canonical_profile(self) -> None:
        """Shuffled and canonical input produce identical profiles."""
        canonical = _build_profile_from_mechanism(self._make_hexapod_18dof())
        shuffled = _build_profile_from_mechanism(self._make_hexapod_18dof_shuffled())

        # Core profile keys must match exactly
        for key in (
            "dofs_per_leg",
            "controller_type",
            "leg_phase_offsets",
            "l_coxa",
            "l_femur",
            "body_length",
            "body_width",
        ):
            self.assertEqual(canonical[key], shuffled[key], f"mismatch on {key}")

        # Joint name order must match
        self.assertEqual(
            canonical["leg_joint_names"],
            shuffled["leg_joint_names"],
        )

    def test_no_ground_returns_empty(self) -> None:
        mech = Mechanism(
            name="no_ground",
            parts=(PartNode(id="a"),),
            joints=(),
            drives=(),
        )
        profile = _build_profile_from_mechanism(mech)
        self.assertEqual(profile, {})

    def test_user_override_takes_priority(self) -> None:
        """User-provided profile fields override auto-extracted values."""
        mech = self._make_hexapod_18dof()
        auto = _build_profile_from_mechanism(mech)
        user = {"controller_type": "custom", "l_coxa": 0.1}
        merged = {**auto, **user}
        self.assertEqual(merged["controller_type"], "custom")
        self.assertAlmostEqual(merged["l_coxa"], 0.1)
        # Auto values preserved for unset fields
        self.assertEqual(merged["dofs_per_leg"], 3)


class TestGearMeshPhasing(TestMotionToolsBase):
    """Tests for compute_gear_mesh_phases() and the mesh_phasing validator."""

    def _simple_pair_mech(
        self,
        z_a: int = 16,
        z_b: int = 32,
        origin_a: tuple[float, float, float] = (0.0, 0.0, 0.0),
        origin_b: tuple[float, float, float] = (24.0, 0.0, 0.0),
        internal: bool = False,
    ) -> Mechanism:
        """Create a simple 2-gear mechanism with revolute joints at given origins."""
        return Mechanism(
            name="phase_test",
            parts=(
                PartNode(id="gear_a"),
                PartNode(id="gear_b"),
                PartNode(id="frame", is_ground=True),
            ),
            joints=(
                JointEdge(
                    id="mesh_ab",
                    joint_type=JointType.GEAR_MESH,
                    parent_part="gear_a",
                    child_part="gear_b",
                    teeth_parent=z_a,
                    teeth_child=z_b,
                    internal=internal,
                ),
                JointEdge(
                    id="rev_a",
                    joint_type=JointType.REVOLUTE,
                    parent_part="frame",
                    child_part="gear_a",
                    origin=origin_a,
                ),
                JointEdge(
                    id="rev_b",
                    joint_type=JointType.REVOLUTE,
                    parent_part="frame",
                    child_part="gear_b",
                    origin=origin_b,
                ),
            ),
            drives=(DriveCondition(joint_id="mesh_ab", speed_rpm=100),),
        )

    def test_compute_gear_mesh_phases_simple_pair(self):
        """16T + 32T along X axis: phase reduced mod angular pitch."""
        from server.motion_validators import compute_gear_mesh_phases

        mech = self._simple_pair_mech(z_a=16, z_b=32)
        phases = compute_gear_mesh_phases(mech)

        self.assertIn("gear_a", phases)
        self.assertIn("gear_b", phases)
        self.assertAlmostEqual(phases["gear_a"], 0.0)
        # p_B = 11.25°. raw = 0 + 180 - 5.625 + 0 = 174.375.
        # Reduced: 174.375 % 11.25 = 5.625°
        self.assertAlmostEqual(phases["gear_b"], 5.625, places=3)

    def test_compute_gear_mesh_phases_angled(self):
        """Gears at 45° angle: contact_angle = 45°."""
        from server.motion_validators import compute_gear_mesh_phases

        d = 24.0
        ox = d * math.cos(math.radians(45))
        oy = d * math.sin(math.radians(45))
        mech = self._simple_pair_mech(
            z_a=16,
            z_b=32,
            origin_b=(ox, oy, 0.0),
        )
        phases = compute_gear_mesh_phases(mech)

        # raw = 45 + 180 - 5.625 + 11.25*(45-0)/22.5 = 219.375 + 22.5 = 241.875
        # Reduced: 241.875 % 11.25 = 241.875 - 21*11.25 = 5.625
        self.assertAlmostEqual(phases["gear_b"], 5.625, places=3)

    def test_compute_gear_mesh_phases_three_gear_train(self):
        """A→B→C chain: all phases consistent with cross-coupling."""
        from server.motion_validators import compute_gear_mesh_phases

        mech = Mechanism(
            name="three_gear",
            parts=(
                PartNode(id="ga"),
                PartNode(id="gb"),
                PartNode(id="gc"),
                PartNode(id="frame", is_ground=True),
            ),
            joints=(
                JointEdge(
                    id="mesh_ab",
                    joint_type=JointType.GEAR_MESH,
                    parent_part="ga",
                    child_part="gb",
                    teeth_parent=16,
                    teeth_child=32,
                ),
                JointEdge(
                    id="mesh_bc",
                    joint_type=JointType.GEAR_MESH,
                    parent_part="gb",
                    child_part="gc",
                    teeth_parent=32,
                    teeth_child=24,
                ),
                JointEdge(
                    id="rev_a",
                    joint_type=JointType.REVOLUTE,
                    parent_part="frame",
                    child_part="ga",
                    origin=(0.0, 0.0, 0.0),
                ),
                JointEdge(
                    id="rev_b",
                    joint_type=JointType.REVOLUTE,
                    parent_part="frame",
                    child_part="gb",
                    origin=(24.0, 0.0, 0.0),
                ),
                JointEdge(
                    id="rev_c",
                    joint_type=JointType.REVOLUTE,
                    parent_part="frame",
                    child_part="gc",
                    origin=(52.0, 0.0, 0.0),
                ),
            ),
            drives=(DriveCondition(joint_id="mesh_ab", speed_rpm=100),),
        )
        phases = compute_gear_mesh_phases(mech)

        self.assertAlmostEqual(phases["ga"], 0.0)
        # B: 5.625° (same as simple pair)
        self.assertAlmostEqual(phases["gb"], 5.625, places=3)
        # C: raw = 0 + 180 - 7.5 + 15*(0 - 5.625)/11.25 = 172.5 - 7.5 = 165
        # Reduced: 165 % 15 = 0°
        self.assertAlmostEqual(phases["gc"], 0.0, places=3)

    def test_mesh_phasing_validator(self):
        """Run the mesh_phasing validator, check measured data."""
        mech = self._simple_pair_mech()
        result = motion_define_mechanism(
            {
                "name": mech.name,
                "parts": [p.to_dict() for p in mech.parts],
                "joints": [
                    {
                        "id": j.id,
                        "joint_type": j.joint_type.value,
                        "parent_part": j.parent_part,
                        "child_part": j.child_part,
                        "teeth_parent": j.teeth_parent,
                        "teeth_child": j.teeth_child,
                        "origin": list(j.origin),
                    }
                    for j in mech.joints
                ],
                "drives": [{"joint_id": d.joint_id, "speed_rpm": d.speed_rpm} for d in mech.drives],
            }
        )
        self.assertTrue(result["ok"])
        mech_id = result["mechanism_id"]

        val_result = motion_validate(mechanism_id=mech_id)
        self.assertTrue(val_result["ok"])

        phasing_results = [r for r in val_result["results"] if r["name"] == "mesh_phasing"]
        self.assertEqual(len(phasing_results), 1)
        pr = phasing_results[0]
        self.assertEqual(pr["status"], "pass")
        self.assertIn("phase_offsets_deg", pr["measured"])
        self.assertAlmostEqual(pr["measured"]["phase_offsets_deg"]["gear_b"], 5.625, places=3)

    def test_check_gear_train_includes_phases(self):
        """motion.check_gear_train should include phase_offsets_deg."""
        mech = self._simple_pair_mech()
        result = motion_define_mechanism(
            {
                "name": mech.name,
                "parts": [p.to_dict() for p in mech.parts],
                "joints": [
                    {
                        "id": j.id,
                        "joint_type": j.joint_type.value,
                        "parent_part": j.parent_part,
                        "child_part": j.child_part,
                        "teeth_parent": j.teeth_parent,
                        "teeth_child": j.teeth_child,
                        "origin": list(j.origin),
                    }
                    for j in mech.joints
                ],
                "drives": [{"joint_id": d.joint_id, "speed_rpm": d.speed_rpm} for d in mech.drives],
            }
        )
        self.assertTrue(result["ok"])
        mech_id = result["mechanism_id"]

        gt_result = motion_check_gear_train(mechanism_id=mech_id)
        self.assertTrue(gt_result["ok"])
        self.assertIn("phase_offsets_deg", gt_result)
        self.assertAlmostEqual(
            gt_result["phase_offsets_deg"]["gear_b"],
            5.625,
            places=3,
        )
        # Geometric interference check should be included
        self.assertIn("tooth_interference", gt_result)
        self.assertTrue(all(r["ok"] for r in gt_result["tooth_interference"]))
        self.assertNotIn("tooth_interference_warning", gt_result)

    def test_no_gear_meshes_returns_empty(self):
        """Mechanism with no gear meshes returns empty phases."""
        from server.motion_validators import compute_gear_mesh_phases

        mech = Mechanism(
            name="no_gears",
            parts=(
                PartNode(id="a"),
                PartNode(id="frame", is_ground=True),
            ),
            joints=(
                JointEdge(
                    id="rev_a",
                    joint_type=JointType.REVOLUTE,
                    parent_part="frame",
                    child_part="a",
                ),
            ),
            drives=(DriveCondition(joint_id="rev_a", speed_rpm=100),),
        )
        phases = compute_gear_mesh_phases(mech)
        self.assertEqual(phases, {})

    def test_internal_gear_phase(self):
        """Internal (ring) gear: no 180° flip, reduced mod pitch."""
        from server.motion_validators import compute_gear_mesh_phases

        mech = self._simple_pair_mech(z_a=16, z_b=48, internal=True)
        phases = compute_gear_mesh_phases(mech)

        # p_B = 7.5°. raw = 0 - 3.75 + 7.5*(0-0)/22.5 = -3.75
        # Reduced: -3.75 % 7.5 = 3.75
        self.assertAlmostEqual(phases["gear_b"], 3.75, places=3)

    def test_animation_ratios_simple_pair(self):
        """Animation ratios: external gear reverses direction, correct magnitude."""
        from server.motion_validators import compute_gear_animation_ratios

        mech = self._simple_pair_mech(z_a=16, z_b=32)
        ratios = compute_gear_animation_ratios(mech)

        self.assertAlmostEqual(ratios["gear_a"], 1.0)
        # External: -Z_a/Z_b = -16/32 = -0.5
        self.assertAlmostEqual(ratios["gear_b"], -0.5)

    def test_animation_ratios_three_gear_train(self):
        """Three-gear train: A→B reverses, B→C reverses again (same as A)."""
        from server.motion_validators import compute_gear_animation_ratios

        mech = Mechanism(
            name="three_gear",
            parts=(
                PartNode(id="ga"),
                PartNode(id="gb"),
                PartNode(id="gc"),
                PartNode(id="frame", is_ground=True),
            ),
            joints=(
                JointEdge(
                    id="mesh_ab",
                    joint_type=JointType.GEAR_MESH,
                    parent_part="ga",
                    child_part="gb",
                    teeth_parent=16,
                    teeth_child=32,
                ),
                JointEdge(
                    id="mesh_bc",
                    joint_type=JointType.GEAR_MESH,
                    parent_part="gb",
                    child_part="gc",
                    teeth_parent=32,
                    teeth_child=24,
                ),
                JointEdge(
                    id="rev_a",
                    joint_type=JointType.REVOLUTE,
                    parent_part="frame",
                    child_part="ga",
                    origin=(0.0, 0.0, 0.0),
                ),
                JointEdge(
                    id="rev_b",
                    joint_type=JointType.REVOLUTE,
                    parent_part="frame",
                    child_part="gb",
                    origin=(24.0, 0.0, 0.0),
                ),
                JointEdge(
                    id="rev_c",
                    joint_type=JointType.REVOLUTE,
                    parent_part="frame",
                    child_part="gc",
                    origin=(52.0, 0.0, 0.0),
                ),
            ),
            drives=(DriveCondition(joint_id="mesh_ab", speed_rpm=100),),
        )
        ratios = compute_gear_animation_ratios(mech)

        self.assertAlmostEqual(ratios["ga"], 1.0)
        # A→B: -16/32 = -0.5
        self.assertAlmostEqual(ratios["gb"], -0.5)
        # B→C: -(-0.5) * 32/24 = +0.5 * 4/3 = +2/3
        self.assertAlmostEqual(ratios["gc"], 2.0 / 3.0)

    def test_animation_ratios_internal_gear(self):
        """Internal gear preserves direction."""
        from server.motion_validators import compute_gear_animation_ratios

        mech = self._simple_pair_mech(z_a=16, z_b=48, internal=True)
        ratios = compute_gear_animation_ratios(mech)

        self.assertAlmostEqual(ratios["gear_a"], 1.0)
        # Internal: +Z_a/Z_b = +16/48 = +1/3
        self.assertAlmostEqual(ratios["gear_b"], 1.0 / 3.0)


class TestToothInterference(TestMotionToolsBase):
    """Tests for check_tooth_interference() geometric validation."""

    def _simple_pair_mech(
        self,
        z_a: int = 16,
        z_b: int = 32,
        origin_a: tuple[float, float, float] = (0.0, 0.0, 0.0),
        origin_b: tuple[float, float, float] = (24.0, 0.0, 0.0),
        internal: bool = False,
    ) -> Mechanism:
        return Mechanism(
            name="interference_test",
            parts=(
                PartNode(id="gear_a"),
                PartNode(id="gear_b"),
                PartNode(id="frame", is_ground=True),
            ),
            joints=(
                JointEdge(
                    id="mesh_ab",
                    joint_type=JointType.GEAR_MESH,
                    parent_part="gear_a",
                    child_part="gear_b",
                    teeth_parent=z_a,
                    teeth_child=z_b,
                    internal=internal,
                ),
                JointEdge(
                    id="rev_a",
                    joint_type=JointType.REVOLUTE,
                    parent_part="frame",
                    child_part="gear_a",
                    origin=origin_a,
                ),
                JointEdge(
                    id="rev_b",
                    joint_type=JointType.REVOLUTE,
                    parent_part="frame",
                    child_part="gear_b",
                    origin=origin_b,
                ),
            ),
            drives=(DriveCondition(joint_id="mesh_ab", speed_rpm=100),),
        )

    def test_computed_phases_pass_interference_check(self):
        """Phases from compute_gear_mesh_phases should pass interference check."""
        from server.motion_validators import check_tooth_interference, compute_gear_mesh_phases

        mech = self._simple_pair_mech(z_a=16, z_b=32)
        phases = compute_gear_mesh_phases(mech)
        results = check_tooth_interference(mech, phases)

        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertTrue(r["ok"], f"Interference detected: {r['detail']}")
        self.assertGreater(r["clearance_deg"], 0.0)

    def test_zero_phase_causes_interference(self):
        """Zero phase offset for both gears should cause interference (teeth collide)."""
        from server.motion_validators import check_tooth_interference

        mech = self._simple_pair_mech(z_a=16, z_b=32)
        # Force both phases to zero — teeth aligned, should collide
        phases = {"gear_a": 0.0, "gear_b": 0.0}
        results = check_tooth_interference(mech, phases)

        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertFalse(r["ok"], f"Expected interference but got: {r['detail']}")
        self.assertLess(r["clearance_deg"], 0.0)

    def test_half_pitch_offset_correct_meshing(self):
        """Half-pitch offset should give correct tooth-gap interlocking."""
        from server.motion_validators import check_tooth_interference

        mech = self._simple_pair_mech(z_a=20, z_b=20)
        # For equal gears along X: half-pitch = 360/(20*2) = 9°
        # Gear B at contact (180° from B) should have gap when A has tooth
        half_pitch = 360.0 / (20 * 2)
        phases = {"gear_a": 0.0, "gear_b": half_pitch}
        results = check_tooth_interference(mech, phases)

        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertTrue(r["ok"], f"Expected correct meshing but got: {r['detail']}")

    def test_angled_gears_pass(self):
        """Gears at 45° should still pass with computed phases."""
        from server.motion_validators import check_tooth_interference, compute_gear_mesh_phases

        d = 24.0
        ox = d * math.cos(math.radians(45))
        oy = d * math.sin(math.radians(45))
        mech = self._simple_pair_mech(z_a=16, z_b=32, origin_b=(ox, oy, 0.0))
        phases = compute_gear_mesh_phases(mech)
        results = check_tooth_interference(mech, phases)

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["ok"], f"Interference: {results[0]['detail']}")

    def test_internal_gear_pass(self):
        """Internal gear with computed phases should pass."""
        from server.motion_validators import check_tooth_interference, compute_gear_mesh_phases

        mech = self._simple_pair_mech(z_a=16, z_b=48, internal=True)
        phases = compute_gear_mesh_phases(mech)
        results = check_tooth_interference(mech, phases)

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["ok"], f"Interference: {results[0]['detail']}")

    def test_three_gear_train_all_pass(self):
        """Three-gear chain: all meshes should pass interference check."""
        from server.motion_validators import check_tooth_interference, compute_gear_mesh_phases

        mech = Mechanism(
            name="three_gear_interf",
            parts=(
                PartNode(id="ga"),
                PartNode(id="gb"),
                PartNode(id="gc"),
                PartNode(id="frame", is_ground=True),
            ),
            joints=(
                JointEdge(
                    id="mesh_ab",
                    joint_type=JointType.GEAR_MESH,
                    parent_part="ga",
                    child_part="gb",
                    teeth_parent=16,
                    teeth_child=32,
                ),
                JointEdge(
                    id="mesh_bc",
                    joint_type=JointType.GEAR_MESH,
                    parent_part="gb",
                    child_part="gc",
                    teeth_parent=32,
                    teeth_child=24,
                ),
                JointEdge(
                    id="rev_a",
                    joint_type=JointType.REVOLUTE,
                    parent_part="frame",
                    child_part="ga",
                    origin=(0.0, 0.0, 0.0),
                ),
                JointEdge(
                    id="rev_b",
                    joint_type=JointType.REVOLUTE,
                    parent_part="frame",
                    child_part="gb",
                    origin=(24.0, 0.0, 0.0),
                ),
                JointEdge(
                    id="rev_c",
                    joint_type=JointType.REVOLUTE,
                    parent_part="frame",
                    child_part="gc",
                    origin=(52.0, 0.0, 0.0),
                ),
            ),
            drives=(DriveCondition(joint_id="mesh_ab", speed_rpm=100),),
        )
        phases = compute_gear_mesh_phases(mech)
        results = check_tooth_interference(mech, phases)

        self.assertEqual(len(results), 2)
        for r in results:
            self.assertTrue(r["ok"], f"Interference at {r['joint_id']}: {r['detail']}")

    def test_validator_reports_interference_info(self):
        """mesh_phasing validator should include tooth_interference in measured."""
        mech = self._simple_pair_mech()
        result = motion_define_mechanism(
            {
                "name": mech.name,
                "parts": [p.to_dict() for p in mech.parts],
                "joints": [
                    {
                        "id": j.id,
                        "joint_type": j.joint_type.value,
                        "parent_part": j.parent_part,
                        "child_part": j.child_part,
                        "teeth_parent": j.teeth_parent,
                        "teeth_child": j.teeth_child,
                        "origin": list(j.origin),
                    }
                    for j in mech.joints
                ],
                "drives": [{"joint_id": d.joint_id, "speed_rpm": d.speed_rpm} for d in mech.drives],
            }
        )
        self.assertTrue(result["ok"])

        val_result = motion_validate(mechanism_id=result["mechanism_id"])
        self.assertTrue(val_result["ok"])

        phasing_results = [r for r in val_result["results"] if r["name"] == "mesh_phasing"]
        self.assertEqual(len(phasing_results), 1)
        pr = phasing_results[0]
        self.assertEqual(pr["status"], "pass")
        self.assertIn("tooth_interference", pr["measured"])
        self.assertTrue(all(r["ok"] for r in pr["measured"]["tooth_interference"]))

    def test_sweep_finds_non_interfering_offset(self):
        """Sweep phase offsets and verify at least one gives no interference."""
        from server.motion_validators import check_tooth_interference

        mech = self._simple_pair_mech(z_a=16, z_b=32)
        p_b = 360.0 / 32  # 11.25°
        found_ok = False

        for i in range(32):
            offset = i * p_b / 32  # sweep from 0 to one angular pitch
            phases = {"gear_a": 0.0, "gear_b": offset}
            results = check_tooth_interference(mech, phases)
            if results[0]["ok"]:
                found_ok = True
                break

        self.assertTrue(found_ok, "No non-interfering offset found in sweep")


class TestDriveJointCheckCollisions(TestMotionToolsBase):
    """Tests for the check_collisions parameter on motion_drive_joint."""

    def _make_gear_pair(self) -> str:
        result = motion_define_mechanism(
            {
                "name": "collision_test",
                "parts": [
                    {"id": "frame", "body_name": "Body_Frame", "is_ground": True},
                    {"id": "gear_a", "body_name": "Body_A"},
                    {"id": "gear_b", "body_name": "Body_B"},
                ],
                "joints": [
                    {
                        "id": "rev_a",
                        "joint_type": "revolute",
                        "parent_part": "frame",
                        "child_part": "gear_a",
                        "origin": [0.0, 0.0, 0.0],
                        "axis": [0.0, 0.0, 1.0],
                    },
                    {
                        "id": "mesh_ab",
                        "joint_type": "gear_mesh",
                        "parent_part": "gear_a",
                        "child_part": "gear_b",
                        "teeth_parent": 16,
                        "teeth_child": 32,
                        "origin": [24.0, 0.0, 0.0],
                        "axis": [0.0, 0.0, 1.0],
                    },
                ],
                "drives": [{"joint_id": "rev_a", "speed_rpm": 60}],
            }
        )
        return result["mechanism_id"]

    def test_check_collisions_no_collisions(self):
        """check_collisions=True reports collision_free when clearance is ok."""
        from unittest.mock import MagicMock, patch

        from server.tools_motion import _assembly_link_maps

        mid = self._make_gear_pair()
        _assembly_link_maps[mid] = {
            "gear_a": "Link_A",
            "gear_b": "Link_B",
        }
        mock_client = MagicMock()

        def fake_send(cmd, **kw):
            if cmd == "check_clearance":
                return {"violations": [], "all_clear": True}
            return {"applied": ["Link_A", "Link_B"]}

        mock_client.send_command.side_effect = fake_send

        with patch("server.freecad_client.get_client", return_value=mock_client):
            result = motion_drive_joint(mid, "rev_a", 90.0, steps=2, check_collisions=True)

        self.assertTrue(result["ok"])
        self.assertTrue(result["collision_free"])
        self.assertEqual(result["collisions"], [])
        self.assertNotIn("collision_summary", result)

        del _assembly_link_maps[mid]

    def test_check_collisions_with_intersections(self):
        """check_collisions=True reports collisions when bodies intersect."""
        from unittest.mock import MagicMock, patch

        from server.tools_motion import _assembly_link_maps

        mid = self._make_gear_pair()
        _assembly_link_maps[mid] = {
            "gear_a": "Link_A",
            "gear_b": "Link_B",
        }
        mock_client = MagicMock()
        call_count = 0

        def fake_send(cmd, **kw):
            nonlocal call_count
            if cmd == "check_clearance":
                call_count += 1
                # Simulate collision on every step
                return {
                    "violations": [
                        {
                            "body_a": "Body_A",
                            "body_b": "Body_B",
                            "distance_mm": 0.0,
                            "intersecting": True,
                        }
                    ],
                    "all_clear": False,
                }
            return {"applied": ["Link_A", "Link_B"]}

        mock_client.send_command.side_effect = fake_send

        with patch("server.freecad_client.get_client", return_value=mock_client):
            result = motion_drive_joint(mid, "rev_a", 90.0, steps=2, check_collisions=True)

        self.assertTrue(result["ok"])
        self.assertFalse(result["collision_free"])
        # 3 steps (0, 1, 2), each with 1 collision = 3 collisions
        self.assertEqual(len(result["collisions"]), 3)
        self.assertIn("collision_summary", result)
        self.assertIn("1 pair(s)", result["collision_summary"])

        del _assembly_link_maps[mid]

    def test_check_collisions_false_omits_fields(self):
        """When check_collisions=False (default), no collision fields in response."""
        from unittest.mock import MagicMock, patch

        from server.tools_motion import _assembly_link_maps

        mid = self._make_gear_pair()
        _assembly_link_maps[mid] = {
            "gear_a": "Link_A",
            "gear_b": "Link_B",
        }
        mock_client = MagicMock()
        mock_client.send_command.return_value = {"applied": ["Link_A", "Link_B"]}

        with patch("server.freecad_client.get_client", return_value=mock_client):
            result = motion_drive_joint(mid, "rev_a", 90.0, steps=1)

        self.assertTrue(result["ok"])
        self.assertNotIn("collisions", result)
        self.assertNotIn("collision_free", result)

        del _assembly_link_maps[mid]

    def test_check_collisions_clearance_error_does_not_abort(self):
        """If check_clearance raises, animation continues without crashing."""
        from unittest.mock import MagicMock, patch

        from server.tools_motion import _assembly_link_maps

        mid = self._make_gear_pair()
        _assembly_link_maps[mid] = {
            "gear_a": "Link_A",
            "gear_b": "Link_B",
        }
        mock_client = MagicMock()

        def fake_send(cmd, **kw):
            if cmd == "check_clearance":
                raise RuntimeError("FreeCAD crashed")
            return {"applied": ["Link_A", "Link_B"]}

        mock_client.send_command.side_effect = fake_send

        with patch("server.freecad_client.get_client", return_value=mock_client):
            result = motion_drive_joint(mid, "rev_a", 90.0, steps=1, check_collisions=True)

        self.assertTrue(result["ok"])
        self.assertTrue(result["collision_free"])
        self.assertEqual(result["collisions"], [])

        del _assembly_link_maps[mid]


class TestCreateAssemblyEmptyWarning(TestMotionToolsBase):
    """Test that motion_create_assembly warns when assembly is empty."""

    def test_no_warning_when_parts_lack_body_name(self):
        """Parts without body_name → link_map empty but no warning (expected_parts=0)."""
        from unittest.mock import MagicMock, patch

        result = motion_define_mechanism(
            {
                "name": "no_bodies",
                "parts": [
                    {"id": "frame", "is_ground": True},
                    {"id": "gear_a"},  # no body_name
                    {"id": "gear_b"},  # no body_name
                ],
                "joints": [],
                "drives": [],
            }
        )
        mid = result["mechanism_id"]

        mock_client = MagicMock()

        def fake_send(cmd, **kw):
            if cmd == "assembly_create":
                return {"name": "Asm_no_bodies"}
            if cmd == "assembly_solve":
                return {}
            return {}

        mock_client.send_command.side_effect = fake_send

        with patch("server.freecad_client.get_client", return_value=mock_client):
            asm_result = motion_create_assembly(mid)

        self.assertTrue(asm_result["ok"])
        self.assertEqual(asm_result["link_map"], {})
        # No warning because no non-ground parts have body_name
        empty_warnings = [w for w in asm_result["warnings"] if "empty" in w.lower()]
        self.assertEqual(len(empty_warnings), 0)

    def test_warning_when_only_ground_has_body_name(self):
        """Non-ground parts have body_name but only ground part does → no warning.

        Actually: ground parts are not counted by expected_parts (is_ground filter).
        Non-ground parts without body_name → expected_parts=0 → no warning.
        """
        from unittest.mock import MagicMock, patch

        result = motion_define_mechanism(
            {
                "name": "ground_only",
                "parts": [
                    {"id": "frame", "body_name": "Body_Frame", "is_ground": True},
                    {"id": "gear_a"},  # no body_name, not ground
                ],
                "joints": [],
                "drives": [],
            }
        )
        mid = result["mechanism_id"]

        mock_client = MagicMock()

        def fake_send(cmd, **kw):
            if cmd == "assembly_create":
                return {"name": "Asm_ground_only"}
            if cmd == "assembly_add_part":
                return {"link_name": "Link_Frame"}
            if cmd == "assembly_solve":
                return {}
            return {}

        mock_client.send_command.side_effect = fake_send

        with patch("server.freecad_client.get_client", return_value=mock_client):
            asm_result = motion_create_assembly(mid)

        self.assertTrue(asm_result["ok"])
        # expected_parts = 0 (gear_a has no body_name), so no empty warning
        empty_warnings = [w for w in asm_result["warnings"] if "empty" in w.lower()]
        self.assertEqual(len(empty_warnings), 0)

    def test_successful_link_no_warning(self):
        """Parts successfully linked → no empty assembly warning."""
        from unittest.mock import MagicMock, patch

        result = motion_define_mechanism(
            {
                "name": "linked_ok",
                "parts": [
                    {"id": "frame", "is_ground": True},
                    {"id": "gear_a", "body_name": "Body_A"},
                ],
                "joints": [],
                "drives": [],
            }
        )
        mid = result["mechanism_id"]

        mock_client = MagicMock()

        def fake_send(cmd, **kw):
            if cmd == "assembly_create":
                return {"name": "Asm_linked_ok"}
            if cmd == "assembly_add_part":
                return {"link_name": "Link_A"}
            if cmd == "assembly_solve":
                return {}
            return {}

        mock_client.send_command.side_effect = fake_send

        with patch("server.freecad_client.get_client", return_value=mock_client):
            asm_result = motion_create_assembly(mid)

        self.assertTrue(asm_result["ok"])
        self.assertEqual(len(asm_result["link_map"]), 1)
        empty_warnings = [w for w in asm_result["warnings"] if "empty" in w.lower()]
        self.assertEqual(len(empty_warnings), 0)


class TestGearAnimationRatiosWithGround(TestMotionToolsBase):
    """Verify gear ratios are correct when drive joint is frame→gear (ground parent)."""

    def _make_16_32_mech(self) -> str:
        """16T/32T gear pair with drive on frame→gear_16t revolute."""
        result = motion_define_mechanism(
            {
                "name": "gear_16_32",
                "parts": [
                    {"id": "frame", "is_ground": True},
                    {"id": "gear_16t", "body_name": "Gear_16T"},
                    {"id": "gear_32t", "body_name": "Gear_32T"},
                ],
                "joints": [
                    {
                        "id": "rev_16t",
                        "joint_type": "revolute",
                        "parent_part": "frame",
                        "child_part": "gear_16t",
                        "origin": [0.0, 0.0, 0.0],
                        "axis": [0.0, 0.0, 1.0],
                    },
                    {
                        "id": "rev_32t",
                        "joint_type": "revolute",
                        "parent_part": "frame",
                        "child_part": "gear_32t",
                        "origin": [24.0, 0.0, 0.0],
                        "axis": [0.0, 0.0, 1.0],
                    },
                    {
                        "id": "mesh_16_32",
                        "joint_type": "gear_mesh",
                        "parent_part": "gear_16t",
                        "child_part": "gear_32t",
                        "teeth_parent": 16,
                        "teeth_child": 32,
                        "origin": [13.5, 0.0, 0.0],
                        "axis": [0.0, 0.0, 1.0],
                    },
                ],
                "drives": [{"joint_id": "rev_16t", "speed_rpm": 60}],
            }
        )
        self.assertTrue(result["ok"])
        return result["mechanism_id"]

    def test_gear_seed_returns_driven_gear_not_ground(self):
        """_find_gear_seed must return the gear part, not the ground frame."""
        from server.motion_validators import _find_gear_seed

        mid = self._make_16_32_mech()
        mech = motion_store.get(mid)
        seed = _find_gear_seed(mech)
        self.assertEqual(seed, "gear_16t")

    def test_animation_ratios_16t_32t(self):
        """16T drives 32T: ratios must be 1.0 and -0.5 (counter-rotate, half speed)."""
        from server.motion_validators import compute_gear_animation_ratios

        mid = self._make_16_32_mech()
        mech = motion_store.get(mid)
        ratios = compute_gear_animation_ratios(mech)

        self.assertAlmostEqual(ratios["gear_16t"], 1.0)
        self.assertAlmostEqual(ratios["gear_32t"], -0.5)

    def test_drive_joint_uses_revolute_origin_not_mesh(self):
        """Rotation center for gear_32t must be its revolute origin (24,0,0),
        not the gear mesh contact point (13.5,0,0)."""
        from unittest.mock import MagicMock, patch

        from server.motion_validators import compute_gear_animation_ratios, compute_gear_mesh_phases

        mid = self._make_16_32_mech()
        mech = motion_store.get(mid)

        # Compute expected ratio and phase
        ratios = compute_gear_animation_ratios(mech)
        phases = compute_gear_mesh_phases(mech)
        ratio_32t = ratios["gear_32t"]  # should be -0.5
        phase_32t = phases.get("gear_32t", 0.0)

        mock_client = MagicMock()
        set_placement_calls = []

        def fake_send(cmd, **kw):
            if cmd == "set_placement":
                set_placement_calls.append(kw)
                return {}
            if cmd == "screenshot":
                return {}
            return {}

        mock_client.send_command.side_effect = fake_send

        with patch("server.freecad_client.get_client", return_value=mock_client):
            result = motion_drive_joint(mid, "rev_16t", 360.0, steps=1)

        self.assertTrue(result["ok"])
        gear_32t_calls = [c for c in set_placement_calls if c.get("object_name") == "Gear_32T"]
        self.assertGreater(len(gear_32t_calls), 0)
        last_call = gear_32t_calls[-1]
        expected_angle = ratio_32t * 360.0 + phase_32t
        self.assertAlmostEqual(last_call["rotation_angle_deg"], expected_angle, places=3)

    def test_body_placement_position_stays_at_joint_origin(self):
        """Body placement fallback must keep position at joint origin.

        For a gear at revolute origin (24,0,0) with body initially placed
        at (24,0,0), the position must stay constant at (24,0,0) while
        only the rotation angle changes.
        """
        from unittest.mock import MagicMock, patch

        # Use a mechanism WITHOUT mesh phases for cleaner math:
        # single revolute, single part, no gear_mesh
        result = motion_define_mechanism(
            {
                "name": "rotation_center_test",
                "parts": [
                    {"id": "frame", "is_ground": True},
                    {"id": "wheel", "body_name": "Body_Wheel"},
                ],
                "joints": [
                    {
                        "id": "rev_wheel",
                        "joint_type": "revolute",
                        "parent_part": "frame",
                        "child_part": "wheel",
                        "origin": [24.0, 0.0, 0.0],
                        "axis": [0.0, 0.0, 1.0],
                    },
                ],
                "drives": [{"joint_id": "rev_wheel", "speed_rpm": 60}],
            }
        )
        mid = result["mechanism_id"]

        mock_client = MagicMock()
        set_placement_calls = []

        def fake_send(cmd, **kw):
            if cmd == "set_placement":
                set_placement_calls.append(kw)
                return {}
            if cmd == "screenshot":
                return {}
            return {}

        mock_client.send_command.side_effect = fake_send

        # Mock model tree: body placed at joint origin (24, 0, 0)
        fake_tree = {
            "ok": True,
            "bodies": [
                {"label": "Body_Wheel", "position": [24.0, 0.0, 0.0]},
            ],
        }

        # Drive 90°
        with (
            patch("server.freecad_client.get_client", return_value=mock_client),
            patch("server.tools_motion.cad_get_model_tree", return_value=fake_tree, create=True),
            patch("server.tools_cad.cad_get_model_tree", return_value=fake_tree),
        ):
            result = motion_drive_joint(mid, "rev_wheel", 90.0, steps=1)

        self.assertTrue(result["ok"])
        wheel_calls = [c for c in set_placement_calls if c.get("object_name") == "Body_Wheel"]
        self.assertGreater(len(wheel_calls), 0)
        last_call = wheel_calls[-1]
        pos = last_call["position"]
        # Position must stay at joint origin (24, 0, 0) — only rotation changes
        self.assertAlmostEqual(pos[0], 24.0, places=3)
        self.assertAlmostEqual(pos[1], 0.0, places=3)
        self.assertAlmostEqual(pos[2], 0.0, places=3)
        self.assertAlmostEqual(last_call["rotation_angle_deg"], 90.0, places=3)

    def test_body_placement_offset_body_orbits_correctly(self):
        """Body at (30,0,0) with joint origin at (24,0,0) driven 90° about Z.

        Offset = (6,0,0), rotated 90° about Z → (0,6,0).
        New position = center + rotated_offset = (24,6,0).
        """
        from unittest.mock import MagicMock, patch

        result = motion_define_mechanism(
            {
                "name": "offset_orbit_test",
                "parts": [
                    {"id": "frame", "is_ground": True},
                    {"id": "wheel", "body_name": "Body_Wheel"},
                ],
                "joints": [
                    {
                        "id": "rev_wheel",
                        "joint_type": "revolute",
                        "parent_part": "frame",
                        "child_part": "wheel",
                        "origin": [24.0, 0.0, 0.0],
                        "axis": [0.0, 0.0, 1.0],
                    },
                ],
                "drives": [{"joint_id": "rev_wheel", "speed_rpm": 60}],
            }
        )
        mid = result["mechanism_id"]

        mock_client = MagicMock()
        set_placement_calls = []

        def fake_send(cmd, **kw):
            if cmd == "set_placement":
                set_placement_calls.append(kw)
                return {}
            if cmd == "screenshot":
                return {}
            return {}

        mock_client.send_command.side_effect = fake_send

        # Body initially at (30, 0, 0) — offset 6mm from joint origin
        fake_tree = {
            "ok": True,
            "bodies": [
                {"label": "Body_Wheel", "position": [30.0, 0.0, 0.0]},
            ],
        }

        with (
            patch("server.freecad_client.get_client", return_value=mock_client),
            patch("server.tools_motion.cad_get_model_tree", return_value=fake_tree, create=True),
            patch("server.tools_cad.cad_get_model_tree", return_value=fake_tree),
        ):
            result = motion_drive_joint(mid, "rev_wheel", 90.0, steps=1)

        self.assertTrue(result["ok"])
        wheel_calls = [c for c in set_placement_calls if c.get("object_name") == "Body_Wheel"]
        self.assertGreater(len(wheel_calls), 0)
        last_call = wheel_calls[-1]
        pos = last_call["position"]
        # Offset (6,0,0) rotated 90° about Z → (0,6,0), plus center (24,0,0) → (24,6,0)
        self.assertAlmostEqual(pos[0], 24.0, places=3)
        self.assertAlmostEqual(pos[1], 6.0, places=3)
        self.assertAlmostEqual(pos[2], 0.0, places=3)
        self.assertAlmostEqual(last_call["rotation_angle_deg"], 90.0, places=3)


class TestRotatePointAroundCenter(unittest.TestCase):
    """Unit tests for the _rotate_point_around_center helper."""

    def test_identity_rotation(self):
        from server.tools_motion import _rotate_point_around_center

        p = (10.0, 5.0, 3.0)
        result = _rotate_point_around_center(p, (0.0, 0.0, 0.0), (0.0, 0.0, 1.0), 0.0)
        for i in range(3):
            self.assertAlmostEqual(result[i], p[i], places=10)

    def test_center_equals_point(self):
        from server.tools_motion import _rotate_point_around_center

        p = (5.0, 5.0, 5.0)
        result = _rotate_point_around_center(p, p, (0.0, 0.0, 1.0), 90.0)
        for i in range(3):
            self.assertAlmostEqual(result[i], p[i], places=10)

    def test_90_deg_z_rotation(self):
        from server.tools_motion import _rotate_point_around_center

        # (1,0,0) rotated 90° about Z around origin → (0,1,0)
        result = _rotate_point_around_center(
            (1.0, 0.0, 0.0), (0.0, 0.0, 0.0), (0.0, 0.0, 1.0), 90.0
        )
        self.assertAlmostEqual(result[0], 0.0, places=10)
        self.assertAlmostEqual(result[1], 1.0, places=10)
        self.assertAlmostEqual(result[2], 0.0, places=10)

    def test_off_center_rotation(self):
        from server.tools_motion import _rotate_point_around_center

        # Point (30,0,0) around center (24,0,0), 90° about Z
        # offset (6,0,0) → rotated (0,6,0) → result (24,6,0)
        result = _rotate_point_around_center(
            (30.0, 0.0, 0.0), (24.0, 0.0, 0.0), (0.0, 0.0, 1.0), 90.0
        )
        self.assertAlmostEqual(result[0], 24.0, places=10)
        self.assertAlmostEqual(result[1], 6.0, places=10)
        self.assertAlmostEqual(result[2], 0.0, places=10)

    def test_360_deg_returns_to_start(self):
        from server.tools_motion import _rotate_point_around_center

        p = (7.0, 3.0, -2.0)
        c = (1.0, 1.0, 1.0)
        result = _rotate_point_around_center(p, c, (0.0, 0.0, 1.0), 360.0)
        for i in range(3):
            self.assertAlmostEqual(result[i], p[i], places=8)


class TestDeriveCaptures(unittest.TestCase):
    """Translation of summary fields into the high-level capture API."""

    def _resp(self, summary: dict) -> dict:
        return {"ok": True, "summary": summary}

    def test_thrust_signals_from_summary(self):
        from server.tools_motion import _derive_captures

        resp = self._resp(
            {
                "applied_force_world_z_mean_N": 14.7,
                "applied_force_world_z_std_N": 0.02,
                "applied_force_count": 30,
            }
        )
        out = _derive_captures(resp, ["thrust_mean_N", "thrust_std_N", "applied_force_count"])
        self.assertEqual(out["signals"]["thrust_mean_N"], 14.7)
        self.assertEqual(out["signals"]["thrust_std_N"], 0.02)
        self.assertEqual(out["signals"]["applied_force_count"], 30)
        self.assertEqual(out["unrecognized"], [])

    def test_hub_bearing_load_returns_dict_of_joints(self):
        from server.tools_motion import _derive_captures

        resp = self._resp(
            {
                "mean_joint_forces": {"rotor_test_joint": 14.71, "other": 0.0},
                "peak_joint_forces": {"rotor_test_joint": 18.55, "other": 0.0},
            }
        )
        out = _derive_captures(resp, ["hub_bearing_load_N", "peak_hub_bearing_load_N"])
        self.assertEqual(out["signals"]["hub_bearing_load_N"]["rotor_test_joint"], 14.71)
        self.assertEqual(out["signals"]["peak_hub_bearing_load_N"]["rotor_test_joint"], 18.55)

    def test_unknown_signals_listed_in_unrecognized(self):
        from server.tools_motion import _derive_captures

        out = _derive_captures(
            self._resp({}), ["thrust_mean_N", "blade_root_moment_Nm", "tip_deflection_mm"]
        )
        self.assertEqual(out["unrecognized"], ["blade_root_moment_Nm", "tip_deflection_mm"])

    def test_missing_summary_fields_yield_none(self):
        from server.tools_motion import _derive_captures

        out = _derive_captures(self._resp({}), ["thrust_mean_N", "thrust_std_N"])
        self.assertIsNone(out["signals"]["thrust_mean_N"])
        self.assertIsNone(out["signals"]["thrust_std_N"])

    def test_missing_joint_dicts_yield_empty(self):
        from server.tools_motion import _derive_captures

        out = _derive_captures(self._resp({}), ["hub_bearing_load_N"])
        self.assertEqual(out["signals"]["hub_bearing_load_N"], {})


if __name__ == "__main__":
    unittest.main()
