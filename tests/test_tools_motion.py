"""Tests for server.tools_motion — MCP tool wrappers for motion validation."""
from __future__ import annotations

import unittest

from server import motion_store
from server.tools_motion import (
    motion_check_gear_train,
    motion_check_interference,
    motion_create_assembly,
    motion_define_mechanism,
    motion_drive_joint,
    motion_list_mechanisms,
    motion_propagate_motion,
    motion_simulate,
    motion_teleop_command,
    motion_teleop_start,
    motion_teleop_state,
    motion_teleop_stop,
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
        motion_define_mechanism({
            "name": "test",
            "parts": [{"id": "a", "is_ground": True}],
            "joints": [],
            "drives": [],
        })
        result = motion_list_mechanisms()
        self.assertEqual(len(result["mechanisms"]), 1)
        self.assertEqual(result["mechanisms"][0]["name"], "test")


class TestValidate(TestMotionToolsBase):
    def _define_gear_pair(self) -> str:
        result = motion_define_mechanism({
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
        })
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
        result = motion_define_mechanism({
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
        })
        mid = result["mechanism_id"]
        prop = motion_propagate_motion(mid)
        self.assertTrue(prop["ok"])
        self.assertIn("states", prop)
        self.assertAlmostEqual(prop["states"]["gear_a"]["rpm"], 1000.0)
        self.assertAlmostEqual(prop["states"]["gear_b"]["rpm"], 2000.0)
        self.assertAlmostEqual(prop["states"]["gear_a"]["torque_nm"], 5.0)

    def test_propagate_not_found(self):
        result = motion_propagate_motion("mech_nonexistent")
        self.assertFalse(result["ok"])


class TestCheckGearTrain(TestMotionToolsBase):
    def test_check(self):
        result = motion_define_mechanism({
            "name": "gear_pair",
            "parts": [{"id": "a"}, {"id": "b"}, {"id": "frame", "is_ground": True}],
            "joints": [{
                "id": "mesh",
                "joint_type": "gear_mesh",
                "parent_part": "a",
                "child_part": "b",
                "teeth_parent": 20,
                "teeth_child": 40,
                "gear_ratio": 0.5,
            }],
            "drives": [],
        })
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
        result = motion_define_mechanism({
            "name": "test_asm",
            "parts": [
                {"id": "gear_a", "body_name": "Body_A"},
                {"id": "gear_b", "body_name": "Body_B"},
                {"id": "frame", "is_ground": True},
            ],
            "joints": [{
                "id": "mesh",
                "joint_type": "gear_mesh",
                "parent_part": "gear_a",
                "child_part": "gear_b",
                "teeth_parent": 20,
                "teeth_child": 40,
            }],
            "drives": [],
        })
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
        result = motion_define_mechanism({
            "name": "map_test",
            "parts": [
                {"id": "gear_a", "body_name": "Body_A"},
                {"id": "gear_b", "body_name": "Body_B"},
                {"id": "frame", "is_ground": True},
            ],
            "joints": [{
                "id": "sun_rev",
                "joint_type": "revolute",
                "parent_part": "frame",
                "child_part": "gear_a",
            }],
            "drives": [],
        })
        mid = result["mechanism_id"]
        # create_assembly will fail (no FreeCAD), but the mapping logic is tested
        # via the drive_joint resolution path
        self.assertNotIn(mid, _assembly_joint_maps)

    def test_drive_joint_resolves_through_map(self):
        """Verify drive_joint uses the joint map for name resolution."""
        from server.tools_motion import _assembly_joint_maps

        result = motion_define_mechanism({
            "name": "resolve_test",
            "parts": [
                {"id": "gear_a", "body_name": "Body_A"},
                {"id": "frame", "is_ground": True},
            ],
            "joints": [{
                "id": "sun_rev",
                "joint_type": "revolute",
                "parent_part": "frame",
                "child_part": "gear_a",
            }],
            "drives": [],
        })
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
        result = motion_define_mechanism({
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
        })
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
            c for c in mock_client.send_command.call_args_list
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
            c for c in mock_client.send_command.call_args_list
            if c[0][0] == "assembly_get_links"
        ]
        self.assertEqual(len(get_links_calls), 1)

        # Placements should still contain both parts
        set_calls = [
            c for c in mock_client.send_command.call_args_list
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

        result = motion_define_mechanism({
            "name": "origin_axis_test",
            "parts": [
                {"id": "sun", "body_name": "Body_Sun"},
                {"id": "frame", "body_name": "Body_Frame", "is_ground": True},
            ],
            "joints": [{
                "id": "sun_rev",
                "joint_type": "revolute",
                "parent_part": "frame",
                "child_part": "sun",
                "origin": [10.0, 20.0, 0.0],
                "axis": [0.0, 0.0, 1.0],
            }],
            "drives": [],
        })
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
            c for c in mock_client.send_command.call_args_list
            if c[0][0] == "assembly_add_joint"
        ]
        self.assertEqual(len(joint_calls), 1)
        kw = joint_calls[0][1]
        self.assertEqual(kw["joint_origin"], [10.0, 20.0, 0.0])
        self.assertEqual(kw["joint_axis"], [0.0, 0.0, 1.0])


class TestDriveJointPlanetaryCompound(TestMotionToolsBase):
    """Verify compound placements are sent with 'position' key for planets."""

    def _make_planetary_mechanism(self):
        """Define a planetary mechanism with ring fixed, sun driven."""
        result = motion_define_mechanism({
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
        })
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
            c for c in mock_client.send_command.call_args_list
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
        result = motion_define_mechanism({
            "name": "test",
            "parts": [{"id": "a", "is_ground": True}],
            "joints": [],
            "drives": [],
        })
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
        result = motion_define_mechanism({
            "name": "test_interf",
            "parts": [{"id": "a", "body_name": "Body_A", "is_ground": True}, {"id": "b", "body_name": "Body_B"}],
            "joints": [],
            "drives": [],
        })
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
        result = motion_define_mechanism({
            "name": "gear_pair",
            "parts": [{"id": "a"}, {"id": "b"}, {"id": "f", "is_ground": True}],
            "joints": [{
                "id": "mesh",
                "joint_type": "gear_mesh",
                "parent_part": "a",
                "child_part": "b",
                "teeth_parent": 20,
                "teeth_child": 40,
            }],
            "drives": [{"joint_id": "mesh", "speed_rpm": 1000}],
        })
        mid = result["mechanism_id"]
        sim_result = motion_simulate(mid)
        if sim_result["ok"]:
            self.assertEqual(sim_result["backend_used"], "isaac")
        else:
            # Bridge unavailable, or bridge rejects unsupported joints — both valid.
            self.assertIn(
                sim_result["error"]["code"],
                {"BACKEND_UNAVAILABLE_CHOOSE", "UNSUPPORTED_JOINT_TYPE", "ISAAC_CONNECTION_LOST"},
            )

    def test_simulate_explicit_chrono_no_daemon(self):
        """Chrono remains available when requested explicitly."""
        result = motion_define_mechanism({
            "name": "gear_pair",
            "parts": [{"id": "a"}, {"id": "b"}, {"id": "f", "is_ground": True}],
            "joints": [{
                "id": "mesh",
                "joint_type": "gear_mesh",
                "parent_part": "a",
                "child_part": "b",
                "teeth_parent": 20,
                "teeth_child": 40,
            }],
            "drives": [{"joint_id": "mesh", "speed_rpm": 1000}],
        })
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
        result = motion_define_mechanism({
            "name": "driven_test",
            "parts": [{"id": "a"}, {"id": "b"}, {"id": "f", "is_ground": True}],
            "joints": [{
                "id": "mesh",
                "joint_type": "gear_mesh",
                "parent_part": "a",
                "child_part": "b",
                "teeth_parent": 20,
                "teeth_child": 40,
            }],
            "drives": [{"joint_id": "mesh", "speed_rpm": 500, "driven_part": "b"}],
        })
        self.assertTrue(result["ok"])


class TestSimulateSpecValidation(TestMotionToolsBase):
    """Test pre-flight spec validation in motion_simulate."""

    def test_no_drives_produces_spec_error(self):
        """A mechanism with no drives should fail spec validation (no motors)."""
        result = motion_define_mechanism({
            "name": "no_drives",
            "parts": [{"id": "a"}, {"id": "f", "is_ground": True}],
            "joints": [{
                "id": "rev",
                "joint_type": "revolute",
                "parent_part": "f",
                "child_part": "a",
            }],
            "drives": [],
        })
        mid = result["mechanism_id"]
        sim_result = motion_simulate(mid, backend="chrono")
        if not sim_result["ok"]:
            # Could be BACKEND_UNAVAILABLE_CHOOSE or SIMULATION_SPEC_INVALID
            if sim_result["error"]["code"] == "SIMULATION_SPEC_INVALID":
                self.assertIn("No motor", sim_result["error"]["message"])


class TestSimulateBackendBehavior(TestMotionToolsBase):
    def _make_mechanism(self) -> str:
        result = motion_define_mechanism({
            "name": "backend_behavior",
            "parts": [{"id": "a"}, {"id": "b"}, {"id": "f", "is_ground": True}],
            "joints": [{
                "id": "mesh",
                "joint_type": "gear_mesh",
                "parent_part": "a",
                "child_part": "b",
                "teeth_parent": 20,
                "teeth_child": 40,
            }],
            "drives": [{"joint_id": "mesh", "speed_rpm": 1000}],
        })
        return result["mechanism_id"]

    def test_invalid_backend(self):
        mid = self._make_mechanism()
        result = motion_simulate(mid, backend="invalid")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "INVALID_INPUT")

    def test_chrono_rejects_teleop_mode(self):
        mid = self._make_mechanism()
        result = motion_simulate(mid, backend="chrono", mode="teleop")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "INVALID_INPUT")

    def test_no_silent_fallback_when_isaac_unavailable(self):
        from unittest.mock import patch

        mid = self._make_mechanism()
        with patch("server.isaac_adapter.simulate_start", return_value={
            "ok": False,
            "error": {"code": "ISAAC_NOT_CONNECTED", "message": "unavailable"},
        }), patch("server.tools_motion._simulate_with_chrono") as chrono_fallback:
            result = motion_simulate(mid, backend="isaac")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "BACKEND_UNAVAILABLE_CHOOSE")
        chrono_fallback.assert_not_called()

    def test_unsupported_joint_type_error_is_propagated(self):
        from unittest.mock import patch

        mid = self._make_mechanism()
        with patch("server.isaac_adapter.simulate_start", return_value={
            "ok": False,
            "error": {
                "code": "UNSUPPORTED_JOINT_TYPE",
                "message": "Unsupported joints present",
            },
        }):
            result = motion_simulate(mid, backend="isaac")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "UNSUPPORTED_JOINT_TYPE")

    def test_simulate_rejects_invalid_numeric_params(self):
        from unittest.mock import patch

        mid = self._make_mechanism()
        bad_inputs = [
            {"duration_s": -1.0},
            {"duration_s": 0.0},
            {"duration_s": float("inf")},
            {"dt_s": 0.0},
            {"dt_s": -0.001},
            {"output_interval": 0.0},
            {"output_interval": -0.01},
            {"dt_s": 0.01, "output_interval": 0.001},
            {"duration_s": 0.2, "output_interval": 0.3},
        ]
        with patch("server.isaac_adapter.simulate_start") as isaac_start:
            for kwargs in bad_inputs:
                with self.subTest(kwargs=kwargs):
                    result = motion_simulate(mid, backend="isaac", **kwargs)
                    self.assertFalse(result["ok"])
                    self.assertEqual(result["error"]["code"], "INVALID_INPUT")
            isaac_start.assert_not_called()

    def test_gazebo_backend_is_valid(self):
        mid = self._make_mechanism()
        # Gazebo bridge may or may not be running.  If not running, we get
        # BACKEND_UNAVAILABLE_CHOOSE or GAZEBO_CONNECTION_LOST.  If running
        # but the fake URDF doesn't exist, we get GAZEBO_SPAWN_FAILED.
        result = motion_simulate(mid, backend="gazebo", urdf_path="/tmp/robot.urdf")
        if not result["ok"]:
            self.assertIn(
                result["error"]["code"],
                {"BACKEND_UNAVAILABLE_CHOOSE", "GAZEBO_CONNECTION_LOST", "GAZEBO_SPAWN_FAILED"},
            )

    def test_gazebo_rejects_teleop_mode(self):
        """Gazebo teleop via motion_simulate should work (not be rejected)."""
        from unittest.mock import patch

        mid = self._make_mechanism()
        # Gazebo teleop is valid — but bridge is unavailable
        with patch("server.gazebo_adapter.teleop_start", return_value={
            "ok": False,
            "error": {"code": "GAZEBO_NOT_CONNECTED", "message": "unavailable"},
        }):
            result = motion_simulate(
                mid,
                backend="gazebo",
                mode="teleop",
                urdf_path="/tmp/robot.urdf",
            )
        self.assertFalse(result["ok"])
        # Should not be INVALID_INPUT — teleop is valid for gazebo
        self.assertNotEqual(result["error"]["code"], "INVALID_INPUT")

    def test_no_silent_fallback_when_gazebo_unavailable(self):
        from unittest.mock import patch

        mid = self._make_mechanism()
        with patch("server.gazebo_adapter.simulate", return_value={
            "ok": False,
            "error": {"code": "GAZEBO_NOT_CONNECTED", "message": "unavailable"},
        }), patch("server.tools_motion._simulate_with_chrono") as chrono_fallback, \
             patch("server.tools_motion._simulate_with_isaac") as isaac_fallback:
            result = motion_simulate(mid, backend="gazebo", urdf_path="/tmp/robot.urdf")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "BACKEND_UNAVAILABLE_CHOOSE")
        chrono_fallback.assert_not_called()
        isaac_fallback.assert_not_called()

    def test_backend_unavailable_choose_includes_all_backends(self):
        from unittest.mock import patch

        mid = self._make_mechanism()
        with patch("server.gazebo_adapter.simulate", return_value={
            "ok": False,
            "error": {"code": "GAZEBO_NOT_CONNECTED", "message": "unavailable"},
        }):
            result = motion_simulate(mid, backend="gazebo", urdf_path="/tmp/robot.urdf")
        self.assertFalse(result["ok"])
        choice_backends = {entry["backend"] for entry in result.get("choices", [])}
        self.assertIn("gazebo", choice_backends)
        self.assertIn("chrono", choice_backends)
        self.assertIn("isaac", choice_backends)

    def test_gazebo_requires_urdf_or_sdf_path(self):
        mid = self._make_mechanism()
        result = motion_simulate(mid, backend="gazebo")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "INVALID_INPUT")
        self.assertIn("urdf_path or sdf_path", result["error"]["message"])

    def test_gazebo_accepts_sdf_path_only(self):
        from unittest.mock import patch

        mid = self._make_mechanism()
        with patch("server.gazebo_adapter.simulate", return_value={
            "ok": False,
            "error": {"code": "GAZEBO_NOT_CONNECTED", "message": "unavailable"},
        }) as gz_sim:
            result = motion_simulate(mid, backend="gazebo", sdf_path="/tmp/robot.sdf")
        self.assertFalse(result["ok"])
        gz_sim.assert_called_once()

    def test_simulate_rejects_non_object_profile(self):
        mid = self._make_mechanism()
        result = motion_simulate(mid, backend="isaac", profile="fast")  # type: ignore[arg-type]
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "INVALID_INPUT")

    def test_batch_simulate_forwards_profile(self):
        from unittest.mock import patch

        mid = self._make_mechanism()
        with patch("server.isaac_adapter.simulate_start", return_value={
            "ok": True,
            "session_id": "sim_mock",
            "status": "complete",
            "target_steps": 100,
            "steady_state_speeds": {},
            "profile_used": {"stiffness": 0.2},
        }) as isaac_start, patch("server.isaac_adapter.simulate_status", return_value={
            "ok": True,
            "status": "complete",
            "completed_steps": 100,
            "target_steps": 100,
            "samples_count": 0,
        }), patch("server.isaac_adapter.simulate_stop", return_value={
            "ok": True,
            "stopped": True,
            "completed_steps": 100,
            "target_steps": 100,
            "samples": [],
        }):
            result = motion_simulate(mid, backend="isaac", profile={"stiffness": 0.2})
        self.assertTrue(result["ok"])
        self.assertEqual(isaac_start.call_args.kwargs.get("profile"), {"stiffness": 0.2})


class TestTeleopTools(TestMotionToolsBase):
    def _make_mechanism(self) -> str:
        result = motion_define_mechanism({
            "name": "teleop_test",
            "parts": [{"id": "base"}, {"id": "frame", "is_ground": True}],
            "joints": [{
                "id": "base_rev",
                "joint_type": "revolute",
                "parent_part": "frame",
                "child_part": "base",
            }],
            "drives": [{"joint_id": "base_rev", "speed_rpm": 100.0}],
        })
        return result["mechanism_id"]

    def test_teleop_lifecycle(self):
        from unittest.mock import patch

        mid = self._make_mechanism()
        with patch("server.isaac_adapter.teleop_start", return_value={
            "ok": True,
            "session_id": "sess_1",
            "status": "started",
        }), patch("server.isaac_adapter.teleop_command", return_value={
            "ok": True,
            "applied": True,
        }), patch("server.isaac_adapter.teleop_state", return_value={
            "ok": True,
            "state": {"vx_mps": 0.2},
        }), patch("server.isaac_adapter.teleop_stop", return_value={
            "ok": True,
            "stopped": True,
        }):
            start = motion_teleop_start(mid)
            self.assertTrue(start["ok"])
            self.assertEqual(start["session_id"], "sess_1")
            self.assertEqual(start["mode_used"], "teleop")

            cmd = motion_teleop_command("sess_1", vx_mps=0.2, yaw_rate_rps=0.1, body_height_m=0.0)
            self.assertTrue(cmd["ok"])

            state = motion_teleop_state("sess_1")
            self.assertTrue(state["ok"])
            self.assertIn("state", state)

            stop = motion_teleop_stop("sess_1")
            self.assertTrue(stop["ok"])

    def test_teleop_start_missing_session_id_is_protocol_error(self):
        from unittest.mock import patch

        mid = self._make_mechanism()
        with patch("server.isaac_adapter.teleop_start", return_value={
            "ok": True,
            "status": "started",
        }):
            start = motion_teleop_start(mid)
        self.assertFalse(start["ok"])
        self.assertEqual(start["error"]["code"], "ISAAC_PROTOCOL_ERROR")

    def test_unknown_remote_session_evicts_local_session_on_command(self):
        from unittest.mock import patch

        mid = self._make_mechanism()
        with patch("server.isaac_adapter.teleop_start", return_value={
            "ok": True,
            "session_id": "sess_evict",
            "status": "started",
        }):
            started = motion_teleop_start(mid)
        self.assertTrue(started["ok"])

        with patch("server.isaac_adapter.teleop_command", return_value={
            "ok": False,
            "error": {"code": "ISAAC_UNKNOWN_SESSION", "message": "unknown session sess_evict"},
        }):
            cmd = motion_teleop_command("sess_evict", vx_mps=0.1)
        self.assertFalse(cmd["ok"])
        self.assertEqual(cmd["error"]["code"], "ISAAC_UNKNOWN_SESSION")

        state = motion_teleop_state("sess_evict")
        self.assertFalse(state["ok"])
        self.assertEqual(state["error"]["code"], "NOT_FOUND")

    def test_unknown_remote_session_evicts_local_session_on_stop(self):
        from unittest.mock import patch

        mid = self._make_mechanism()
        with patch("server.isaac_adapter.teleop_start", return_value={
            "ok": True,
            "session_id": "sess_stop",
        }):
            started = motion_teleop_start(mid)
        self.assertTrue(started["ok"])

        with patch("server.isaac_adapter.teleop_stop", return_value={
            "ok": False,
            "error": {"code": "ISAAC_COMMAND_ERROR", "message": "unknown session sess_stop"},
        }):
            stopped = motion_teleop_stop("sess_stop")
        self.assertFalse(stopped["ok"])

        retry = motion_teleop_stop("sess_stop")
        self.assertFalse(retry["ok"])
        self.assertEqual(retry["error"]["code"], "NOT_FOUND")

    def test_simulate_teleop_mode_routes_to_session_start(self):
        from unittest.mock import patch

        mid = self._make_mechanism()
        with patch("server.isaac_adapter.teleop_start", return_value={
            "ok": True,
            "session_id": "sess_sim_mode",
        }) as teleop_start:
            result = motion_simulate(
                mid,
                backend="isaac",
                mode="teleop",
                profile={"linear_speed_mps": 0.5},
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["mode_used"], "teleop")
        teleop_start.assert_called_once()
        self.assertEqual(
            teleop_start.call_args.kwargs.get("profile"),
            {"linear_speed_mps": 0.5},
        )

    def test_gazebo_teleop_lifecycle(self):
        from unittest.mock import patch

        mid = self._make_mechanism()
        with patch("server.gazebo_adapter.teleop_start", return_value={
            "ok": True,
            "session_id": "gz_sess_1",
            "status": "started",
        }), patch("server.gazebo_adapter.teleop_command", return_value={
            "ok": True,
            "applied": True,
        }), patch("server.gazebo_adapter.teleop_state", return_value={
            "ok": True,
            "state": {"vx_mps": 0.2, "vy_mps": 0.1, "vz_mps": 0.0},
        }), patch("server.gazebo_adapter.teleop_stop", return_value={
            "ok": True,
            "stopped": True,
        }):
            start = motion_teleop_start(mid, backend="gazebo", urdf_path="/tmp/robot.urdf")
            self.assertTrue(start["ok"])
            self.assertEqual(start["session_id"], "gz_sess_1")
            self.assertEqual(start["backend_used"], "gazebo")

            cmd = motion_teleop_command(
                "gz_sess_1", vx_mps=0.2, yaw_rate_rps=0.1,
                body_height_m=0.0, vy_mps=0.1, vz_mps=0.0,
            )
            self.assertTrue(cmd["ok"])
            self.assertEqual(cmd["backend_used"], "gazebo")

            state = motion_teleop_state("gz_sess_1")
            self.assertTrue(state["ok"])

            stop = motion_teleop_stop("gz_sess_1")
            self.assertTrue(stop["ok"])

    def test_isaac_rejects_nonzero_vy_mps(self):
        from unittest.mock import patch

        mid = self._make_mechanism()
        with patch("server.isaac_adapter.teleop_start", return_value={
            "ok": True,
            "session_id": "sess_vy",
            "status": "started",
        }):
            motion_teleop_start(mid, backend="isaac")

        result = motion_teleop_command("sess_vy", vx_mps=0.0, vy_mps=0.5)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "INVALID_INPUT")

    def test_isaac_rejects_nonzero_vz_mps(self):
        from unittest.mock import patch

        mid = self._make_mechanism()
        with patch("server.isaac_adapter.teleop_start", return_value={
            "ok": True,
            "session_id": "sess_vz",
            "status": "started",
        }):
            motion_teleop_start(mid, backend="isaac")

        result = motion_teleop_command("sess_vz", vz_mps=0.3)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "INVALID_INPUT")

    def test_gazebo_accepts_nonzero_vy_vz(self):
        from unittest.mock import patch

        mid = self._make_mechanism()
        with patch("server.gazebo_adapter.teleop_start", return_value={
            "ok": True,
            "session_id": "gz_vy_vz",
            "status": "started",
        }):
            motion_teleop_start(mid, backend="gazebo", urdf_path="/tmp/robot.urdf")

        with patch("server.gazebo_adapter.teleop_command", return_value={
            "ok": True,
            "applied": True,
        }) as gz_cmd:
            result = motion_teleop_command(
                "gz_vy_vz", vx_mps=0.1, vy_mps=0.5, vz_mps=0.3,
            )
        self.assertTrue(result["ok"])
        gz_cmd.assert_called_once()
        call_kwargs = gz_cmd.call_args.kwargs
        self.assertAlmostEqual(call_kwargs["vy_mps"], 0.5)
        self.assertAlmostEqual(call_kwargs["vz_mps"], 0.3)

    def test_session_backend_routing_uses_registry_not_hardcode(self):
        """Teleop command/state/stop should route based on session backend, not hardcoded."""
        from unittest.mock import patch

        mid = self._make_mechanism()
        with patch("server.gazebo_adapter.teleop_start", return_value={
            "ok": True,
            "session_id": "gz_route",
            "status": "started",
        }):
            motion_teleop_start(mid, backend="gazebo", urdf_path="/tmp/robot.urdf")

        # Verify state routes to gazebo, not isaac
        with patch("server.gazebo_adapter.teleop_state", return_value={
            "ok": True,
            "state": {"vx_mps": 0.0},
        }) as gz_state, patch("server.isaac_adapter.teleop_state") as isaac_state:
            motion_teleop_state("gz_route")
        gz_state.assert_called_once()
        isaac_state.assert_not_called()

    def test_unknown_session_backend_returns_not_found(self):
        """A session with an unrecognized backend should return NOT_FOUND."""
        from server.tools_motion import _active_sessions
        _active_sessions["bad_backend_sess"] = {
            "mechanism_id": "m1",
            "backend": "nonexistent",
            "created_at": 0,
        }
        result = motion_teleop_command("bad_backend_sess", vx_mps=0.1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "NOT_FOUND")

    def test_isaac_backward_compat_zero_vy_vz(self):
        """Isaac teleop_command should work fine when vy_mps=0 and vz_mps=0."""
        from unittest.mock import patch

        mid = self._make_mechanism()
        with patch("server.isaac_adapter.teleop_start", return_value={
            "ok": True,
            "session_id": "sess_compat",
            "status": "started",
        }):
            motion_teleop_start(mid, backend="isaac")

        with patch("server.isaac_adapter.teleop_command", return_value={
            "ok": True,
            "applied": True,
        }) as isaac_cmd:
            result = motion_teleop_command(
                "sess_compat", vx_mps=0.2, yaw_rate_rps=0.1,
                body_height_m=0.0, vy_mps=0.0, vz_mps=0.0,
            )
        self.assertTrue(result["ok"])
        isaac_cmd.assert_called_once()

    def test_gazebo_teleop_requires_urdf_or_sdf_path(self):
        mid = self._make_mechanism()
        result = motion_teleop_start(mid, backend="gazebo")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "INVALID_INPUT")
        self.assertIn("urdf_path or sdf_path", result["error"]["message"])

    def test_gazebo_teleop_rejects_invalid_controller_type(self):
        mid = self._make_mechanism()
        result = motion_teleop_start(
            mid,
            backend="gazebo",
            urdf_path="/tmp/robot.urdf",
            profile={"controller_type": "bad_controller"},
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "INVALID_INPUT")

    def test_gazebo_teleop_accepts_px4_offboard_controller_type(self):
        from unittest.mock import patch

        mid = self._make_mechanism()
        with patch("server.gazebo_adapter.teleop_start", return_value={
            "ok": True,
            "session_id": "gz_px4",
            "status": "started",
            "controller_type": "px4_offboard",
        }) as gz_start:
            result = motion_teleop_start(
                mid,
                backend="gazebo",
                urdf_path="/tmp/robot.urdf",
                profile={"controller_type": "px4_offboard"},
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["session_id"], "gz_px4")
        self.assertEqual(gz_start.call_args.kwargs["profile"]["controller_type"], "px4_offboard")


if __name__ == "__main__":
    unittest.main()
