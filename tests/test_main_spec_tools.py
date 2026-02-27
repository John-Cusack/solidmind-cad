from __future__ import annotations

import unittest

from server import main as mcp_main
from tests.helpers import make_base_spec_draft


class TestMainSpecTools(unittest.TestCase):
    def test_tools_list_includes_spec_tools(self) -> None:
        names = {entry.get("name") for entry in mcp_main._tool_list()}
        expected = {
            "spec.select_schema",
            "spec.apply_answer",
            "spec.validate",
            "spec.next_question",
            "spec.finalize",
            "spec.export_brief",
            "spec.export_rfq_summary",
            "spec.assess_design_path",
            "spec.generate_cad",
        }
        self.assertTrue(expected.issubset(names))

    def test_tools_list_includes_motion_teleop_tools(self) -> None:
        names = {entry.get("name") for entry in mcp_main._tool_list()}
        expected = {
            "motion.simulate",
            "motion.teleop_start",
            "motion.teleop_command",
            "motion.teleop_state",
            "motion.teleop_stop",
        }
        self.assertTrue(expected.issubset(names))

    def test_motion_simulate_schema_has_backend_mode(self) -> None:
        tool = next(t for t in mcp_main._tool_list() if t.get("name") == "motion.simulate")
        props = tool["inputSchema"]["properties"]
        self.assertIn("backend", props)
        self.assertIn("mode", props)
        self.assertIn("profile", props)
        self.assertIn("sdf_path", props)
        self.assertEqual(props["backend"]["default"], "isaac")
        self.assertEqual(props["mode"]["default"], "batch")
        self.assertEqual(props["duration_s"]["exclusiveMinimum"], 0)
        self.assertEqual(props["dt_s"]["exclusiveMinimum"], 0)
        self.assertEqual(props["output_interval"]["exclusiveMinimum"], 0)

    def test_call_tool_spec_select_schema(self) -> None:
        out = mcp_main._call_tool(
            "spec.select_schema",
            {"process": "print_3d", "maturity_level": "L2", "spec_version": "1.0.0"},
        )
        self.assertEqual(out["errors"], [])
        self.assertEqual(out["schema_id"], "print_3d_v1")
        self.assertEqual(out["question_bank_id"], "print_3d_v1")
        self.assertEqual(out["coverage_threshold"], 0.8)

    def test_call_tool_spec_apply_answer(self) -> None:
        draft = make_base_spec_draft(process="print_3d", maturity_level="L1")
        out = mcp_main._call_tool(
            "spec.apply_answer",
            {
                "spec_draft": draft,
                "op": "set",
                "path": "/part/envelope",
                "value": {"x": 120, "y": 60, "z": 40},
                "question_id": "envelope",
                "source": "user",
            },
        )
        self.assertTrue(out["applied"])
        self.assertEqual(out["errors"], [])
        updated = out["spec_draft_updated"]
        self.assertEqual(updated["part"]["envelope"], {"x": 120, "y": 60, "z": 40})
        self.assertIn("envelope", updated["_interview"]["answered"])

    def test_call_tool_spec_next_question(self) -> None:
        draft = make_base_spec_draft(process="print_3d", maturity_level="L2")
        draft["part"]["envelope"] = {"x": 40, "y": 30, "z": 20}
        out = mcp_main._call_tool("spec.next_question", {"spec_draft": draft})
        self.assertEqual(out["question_id"], "material_grade")

    def test_call_tool_spec_finalize(self) -> None:
        draft = make_base_spec_draft(process="print_3d", maturity_level="L1")
        draft["part"]["envelope"] = {"x": 40, "y": 30, "z": 20}
        out = mcp_main._call_tool("spec.finalize", {"spec_draft": draft})
        self.assertIn("spec", out)
        self.assertIn("hash", out)
        self.assertNotIn("_interview", out["spec"])
        self.assertNotIn("_audit", out["spec"])

    def test_call_tool_spec_plan_geometry_with_options(self) -> None:
        spec = {
            "meta": {"process": "cnc", "units": "mm"},
            "part": {"envelope": {"x": 100, "y": 50, "z": 20}},
            "geometry": {},
        }
        out = mcp_main._call_tool(
            "spec.plan_geometry",
            {"spec": spec, "options": {"planning_mode": "policy_v1"}},
        )
        self.assertIn("gir", out)
        self.assertIn("eir", out)
        self.assertIn("planning_plan", out)

    def test_call_tool_spec_generate_cad_policy_v1_metadata(self) -> None:
        spec = {
            "meta": {"process": "cnc", "units": "mm", "maturity_level": "L2"},
            "part": {"envelope": {"x": 100, "y": 50, "z": 20}},
            "geometry": {
                "hole_features": [
                    {
                        "id": "h1",
                        "diameter": {"value": 5, "unit": "mm"},
                        "depth": {"value": 20, "unit": "mm"},
                    }
                ]
            },
        }
        out = mcp_main._call_tool(
            "spec.generate_cad",
            {
                "spec": spec,
                "output_format": "step",
                "options": {"planning_mode": "policy_v1"},
            },
        )
        self.assertEqual(out["errors"], [])
        self.assertIn("planning_plan_hash", out["metadata"])
        self.assertIn("policy_key", out["metadata"])
        self.assertIn("checkpoint_summary", out["metadata"])
        self.assertIn("repair_recommendations_present", out["metadata"])


    def test_motion_simulate_backend_enum_includes_gazebo(self) -> None:
        tool = next(t for t in mcp_main._tool_list() if t.get("name") == "motion.simulate")
        backend_enum = tool["inputSchema"]["properties"]["backend"]["enum"]
        self.assertIn("gazebo", backend_enum)
        self.assertIn("isaac", backend_enum)
        self.assertIn("chrono", backend_enum)

    def test_motion_teleop_start_backend_enum_includes_gazebo(self) -> None:
        tool = next(t for t in mcp_main._tool_list() if t.get("name") == "motion.teleop_start")
        backend_enum = tool["inputSchema"]["properties"]["backend"]["enum"]
        self.assertIn("gazebo", backend_enum)
        self.assertIn("isaac", backend_enum)
        self.assertIn("sdf_path", tool["inputSchema"]["properties"])

    def test_motion_teleop_command_schema_has_vy_vz(self) -> None:
        tool = next(t for t in mcp_main._tool_list() if t.get("name") == "motion.teleop_command")
        props = tool["inputSchema"]["properties"]
        self.assertIn("vy_mps", props)
        self.assertIn("vz_mps", props)
        self.assertEqual(props["vy_mps"]["type"], "number")
        self.assertEqual(props["vz_mps"]["type"], "number")
        self.assertEqual(props["vy_mps"]["default"], 0.0)
        self.assertEqual(props["vz_mps"]["default"], 0.0)

    def test_cad_export_sim_package_schema_has_emit_sdf(self) -> None:
        tool = next(t for t in mcp_main._tool_list() if t.get("name") == "cad.export_sim_package")
        props = tool["inputSchema"]["properties"]
        self.assertIn("emit_sdf", props)
        self.assertEqual(props["emit_sdf"]["type"], "boolean")
        self.assertFalse(props["emit_sdf"]["default"])


class TestDesignGenerateMechanism(unittest.TestCase):
    """Tests for design.generate_mechanism and enhanced design.verify_build."""

    def setUp(self) -> None:
        from server import design_store, motion_store
        design_store.clear()
        motion_store.clear()

    def tearDown(self) -> None:
        from server import design_store, motion_store
        design_store.clear()
        motion_store.clear()

    def _make_drone_brief(self) -> str:
        """Create a drone brief with parts and interfaces, return brief_id."""
        result = mcp_main._call_tool("design.save_brief", {
            "name": "Test Drone",
            "parameters": {
                "layout": {
                    "motor_positions": [[77.8, 77.8, 8], [-77.8, 77.8, 8],
                                        [-77.8, -77.8, 8], [77.8, -77.8, 8]],
                },
            },
            "status": "layout",
        })
        brief_id = result["brief"]["brief_id"]

        # Add parts
        mcp_main._call_tool("design.add_part", {
            "brief_id": brief_id, "name": "frame",
            "specs": {"material": "CF 2mm", "mass_g": 80},
        })
        mcp_main._call_tool("design.add_part", {
            "brief_id": brief_id, "name": "motor_mount",
            "quantity": 4,
            "specs": {"mass_g": 5},
        })
        mcp_main._call_tool("design.add_part", {
            "brief_id": brief_id, "name": "motor",
            "kind": "purchased", "quantity": 4,
            "specs": {"model": "Emax 2306", "mass_g": 33},
        })

        # Add interfaces
        mcp_main._call_tool("design.add_interface", {
            "brief_id": brief_id,
            "part_a": "frame", "port_a": "arm_tip",
            "part_b": "motor_mount", "port_b": "base",
            "spec": {"type": "clamp", "tube_od_mm": 10},
        })
        mcp_main._call_tool("design.add_interface", {
            "brief_id": brief_id,
            "part_a": "motor_mount", "port_a": "top",
            "part_b": "motor", "port_b": "base",
            "spec": {"pattern": "M3_16mm_square", "bolt_size": "M3"},
        })

        return brief_id

    def test_tool_registered(self) -> None:
        names = {entry.get("name") for entry in mcp_main._tool_list()}
        self.assertIn("design.generate_mechanism", names)

    def test_generate_mechanism_basic(self) -> None:
        brief_id = self._make_drone_brief()
        result = mcp_main._call_tool("design.generate_mechanism", {
            "brief_id": brief_id,
        })

        self.assertTrue(result["ok"])
        mech = result["mechanism"]

        # Should have 3 parts (frame, motor_mount, motor)
        self.assertEqual(len(mech["parts"]), 3)

        # Should have 2 joints (frame→motor_mount, motor_mount→motor)
        self.assertEqual(len(mech["joints"]), 2)

        # Both should be fixed (clamp and bolt pattern)
        for joint in mech["joints"]:
            self.assertEqual(joint["joint_type"], "fixed")

        # Summary should report counts
        self.assertEqual(result["summary"]["part_count"], 3)
        self.assertEqual(result["summary"]["joint_count"], 2)
        self.assertEqual(result["summary"]["ground_part"], "frame")

    def test_generate_mechanism_ground_part(self) -> None:
        brief_id = self._make_drone_brief()
        result = mcp_main._call_tool("design.generate_mechanism", {
            "brief_id": brief_id,
            "ground_part": "motor_mount",
        })

        self.assertTrue(result["ok"])
        parts = result["mechanism"]["parts"]
        ground = [p for p in parts if p["is_ground"]]
        self.assertEqual(len(ground), 1)
        self.assertEqual(ground[0]["id"], "motor_mount")

    def test_generate_mechanism_revolute_joint(self) -> None:
        """Bearing/shaft interfaces should produce revolute joints."""
        from server import design_store
        result = mcp_main._call_tool("design.save_brief", {
            "name": "Hinge Test",
            "parameters": {},
        })
        brief_id = result["brief"]["brief_id"]

        mcp_main._call_tool("design.add_part", {
            "brief_id": brief_id, "name": "bracket",
        })
        mcp_main._call_tool("design.add_part", {
            "brief_id": brief_id, "name": "door",
        })
        mcp_main._call_tool("design.add_interface", {
            "brief_id": brief_id,
            "part_a": "bracket", "port_a": "hinge_pin",
            "part_b": "door", "port_b": "hinge_knuckle",
            "spec": {"type": "hinge", "pin_diameter_mm": 6},
        })

        result = mcp_main._call_tool("design.generate_mechanism", {
            "brief_id": brief_id,
        })
        self.assertTrue(result["ok"])
        joints = result["mechanism"]["joints"]
        self.assertEqual(len(joints), 1)
        self.assertEqual(joints[0]["joint_type"], "revolute")

    def test_generate_mechanism_prismatic_joint(self) -> None:
        """Slider/rail interfaces should produce prismatic joints."""
        result = mcp_main._call_tool("design.save_brief", {
            "name": "Slider Test",
            "parameters": {},
        })
        brief_id = result["brief"]["brief_id"]

        mcp_main._call_tool("design.add_part", {
            "brief_id": brief_id, "name": "base",
        })
        mcp_main._call_tool("design.add_part", {
            "brief_id": brief_id, "name": "carriage",
        })
        mcp_main._call_tool("design.add_interface", {
            "brief_id": brief_id,
            "part_a": "base", "port_a": "rail",
            "part_b": "carriage", "port_b": "slider",
            "spec": {"type": "linear_rail", "rail_width_mm": 12},
        })

        result = mcp_main._call_tool("design.generate_mechanism", {
            "brief_id": brief_id,
        })
        self.assertTrue(result["ok"])
        joints = result["mechanism"]["joints"]
        self.assertEqual(len(joints), 1)
        self.assertEqual(joints[0]["joint_type"], "prismatic")

    def test_generate_mechanism_no_parts(self) -> None:
        result = mcp_main._call_tool("design.save_brief", {
            "name": "Empty", "parameters": {},
        })
        brief_id = result["brief"]["brief_id"]
        result = mcp_main._call_tool("design.generate_mechanism", {
            "brief_id": brief_id,
        })
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "NO_PARTS")

    def test_generate_mechanism_no_interfaces(self) -> None:
        result = mcp_main._call_tool("design.save_brief", {
            "name": "Parts Only", "parameters": {},
        })
        brief_id = result["brief"]["brief_id"]
        mcp_main._call_tool("design.add_part", {
            "brief_id": brief_id, "name": "plate",
        })
        result = mcp_main._call_tool("design.generate_mechanism", {
            "brief_id": brief_id,
        })
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "NO_INTERFACES")

    def test_generate_mechanism_brief_not_found(self) -> None:
        result = mcp_main._call_tool("design.generate_mechanism", {
            "brief_id": "brief_nonexistent",
        })
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "BRIEF_NOT_FOUND")

    def test_generate_mechanism_motor_position_origin(self) -> None:
        """Joint origin should use layout motor_positions for motor-related parts."""
        brief_id = self._make_drone_brief()
        result = mcp_main._call_tool("design.generate_mechanism", {
            "brief_id": brief_id,
        })
        self.assertTrue(result["ok"])

        # The motor_mount→motor joint should pick up motor_positions
        joints = result["mechanism"]["joints"]
        motor_joint = next(j for j in joints if j["child_part"] == "motor")
        self.assertEqual(motor_joint["origin"], [77.8, 77.8, 8.0])

    def test_verify_build_with_mechanism_missing_joint(self) -> None:
        """verify_build with mechanism_id should flag unconnected interfaces."""
        from server import motion_store
        from server.motion_models import Mechanism, PartNode, JointEdge, JointType

        brief_id = self._make_drone_brief()

        # Create a mechanism with only ONE joint (frame→motor_mount),
        # missing the motor_mount→motor joint
        mech = Mechanism(
            name="partial",
            parts=(
                PartNode(id="frame", is_ground=True),
                PartNode(id="motor_mount"),
                PartNode(id="motor"),
            ),
            joints=(
                JointEdge(
                    id="j1", joint_type=JointType.FIXED,
                    parent_part="frame", child_part="motor_mount",
                ),
            ),
            drives=(),
        )
        mech_id = motion_store.store(mech)

        # verify_build requires FreeCAD model tree — mock it
        import server.tools_cad as tc
        original = tc.cad_get_model_tree

        def mock_tree(doc=None, detail="bodies"):
            return {"ok": True, "bodies": [
                {"label": "frame", "name": "Body", "size": [100, 100, 2]},
                {"label": "motor_mount", "name": "Body001", "size": [20, 20, 5]},
            ]}

        tc.cad_get_model_tree = mock_tree
        try:
            result = mcp_main._call_tool("design.verify_build", {
                "brief_id": brief_id,
                "mechanism_id": mech_id,
            })
        finally:
            tc.cad_get_model_tree = original

        self.assertTrue(result["ok"])
        # Should have an interface warning for motor_mount↔motor
        self.assertIn("interface_warnings", result)
        warnings = result["interface_warnings"]
        self.assertEqual(len(warnings), 1)
        self.assertIn("motor_mount", warnings[0])
        self.assertIn("motor", warnings[0])

    def test_verify_build_without_mechanism_no_warnings(self) -> None:
        """verify_build without mechanism_id should have no interface_warnings key."""
        brief_id = self._make_drone_brief()

        import server.tools_cad as tc
        original = tc.cad_get_model_tree

        def mock_tree(doc=None, detail="bodies"):
            return {"ok": True, "bodies": [
                {"label": "frame", "name": "Body", "size": [100, 100, 2]},
                {"label": "motor_mount", "name": "Body001", "size": [20, 20, 5]},
            ]}

        tc.cad_get_model_tree = mock_tree
        try:
            result = mcp_main._call_tool("design.verify_build", {
                "brief_id": brief_id,
            })
        finally:
            tc.cad_get_model_tree = original

        self.assertTrue(result["ok"])
        self.assertNotIn("interface_warnings", result)

    def test_verify_build_all_joints_covered(self) -> None:
        """verify_build with all joints covered should have no warnings."""
        from server import motion_store
        from server.motion_models import Mechanism, PartNode, JointEdge, JointType

        brief_id = self._make_drone_brief()

        # Create a mechanism covering BOTH interfaces
        mech = Mechanism(
            name="full",
            parts=(
                PartNode(id="frame", is_ground=True),
                PartNode(id="motor_mount"),
                PartNode(id="motor"),
            ),
            joints=(
                JointEdge(
                    id="j1", joint_type=JointType.FIXED,
                    parent_part="frame", child_part="motor_mount",
                ),
                JointEdge(
                    id="j2", joint_type=JointType.FIXED,
                    parent_part="motor_mount", child_part="motor",
                ),
            ),
            drives=(),
        )
        mech_id = motion_store.store(mech)

        import server.tools_cad as tc
        original = tc.cad_get_model_tree

        def mock_tree(doc=None, detail="bodies"):
            return {"ok": True, "bodies": [
                {"label": "frame", "name": "Body", "size": [100, 100, 2]},
                {"label": "motor_mount", "name": "Body001", "size": [20, 20, 5]},
            ]}

        tc.cad_get_model_tree = mock_tree
        try:
            result = mcp_main._call_tool("design.verify_build", {
                "brief_id": brief_id,
                "mechanism_id": mech_id,
            })
        finally:
            tc.cad_get_model_tree = original

        self.assertTrue(result["ok"])
        self.assertNotIn("interface_warnings", result)

    def test_verify_build_schema_has_mechanism_id(self) -> None:
        tool = next(t for t in mcp_main._tool_list()
                    if t.get("name") == "design.verify_build")
        props = tool["inputSchema"]["properties"]
        self.assertIn("mechanism_id", props)
        self.assertEqual(props["mechanism_id"]["type"], "string")

    def test_generate_mechanism_schema(self) -> None:
        tool = next(t for t in mcp_main._tool_list()
                    if t.get("name") == "design.generate_mechanism")
        props = tool["inputSchema"]["properties"]
        self.assertIn("brief_id", props)
        self.assertIn("ground_part", props)
        self.assertIn("brief_id", tool["inputSchema"]["required"])


if __name__ == "__main__":
    unittest.main()
