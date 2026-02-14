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
    motion_validate,
)


class TestMotionToolsBase(unittest.TestCase):
    def setUp(self):
        motion_store.clear()

    def tearDown(self):
        motion_store.clear()


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

    def test_define_warns_on_unknown_part(self):
        d = self._gear_pair_dict()
        d["joints"][0]["parent_part"] = "nonexistent"
        result = motion_define_mechanism(d)
        self.assertTrue(result["ok"])  # Still succeeds, but with warnings
        self.assertGreater(len(result["warnings"]), 0)

    def test_define_warns_no_ground(self):
        d = self._gear_pair_dict()
        for p in d["parts"]:
            p["is_ground"] = False
        result = motion_define_mechanism(d)
        self.assertTrue(result["ok"])
        self.assertTrue(any("ground" in w.lower() for w in result["warnings"]))

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
            "parts": [{"id": "a"}],
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
            "parts": [{"id": "a"}, {"id": "b"}],
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


class TestDriveJoint(TestMotionToolsBase):
    def test_drive_joint_not_found_mechanism(self):
        result = motion_drive_joint("mech_nonexistent", "j1", 360.0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "NOT_FOUND")

    def test_drive_joint_not_found_joint(self):
        result = motion_define_mechanism({
            "name": "test",
            "parts": [{"id": "a"}],
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
            "parts": [{"id": "a", "body_name": "Body_A"}, {"id": "b", "body_name": "Body_B"}],
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

    def test_simulate_no_daemon(self):
        """Without a running Chrono daemon, simulate returns a clear error."""
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
        self.assertFalse(sim_result["ok"])
        self.assertEqual(sim_result["error"]["code"], "CHRONO_NOT_CONNECTED")
        self.assertIn("chrono_daemon", sim_result["error"]["message"].lower())


if __name__ == "__main__":
    unittest.main()
