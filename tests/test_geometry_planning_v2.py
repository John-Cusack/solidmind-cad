from __future__ import annotations

import unittest

from server.geometry_ir import (
    EIRBuilder,
    GIRBuilder,
    Invariant,
    Quantity,
    Vector3D,
)
from server.geometry_planning import plan_geometry, _generate_eir


class TestPlanGeometryBoxBasic(unittest.TestCase):
    """Test GIR extraction for box-type envelope specs."""

    def test_box_envelope_produces_sketch_and_extrude(self) -> None:
        spec = {
            "envelope": {
                "length": {"value": 100, "unit": "mm"},
                "width": {"value": 50, "unit": "mm"},
                "height": {"value": 20, "unit": "mm"},
            },
        }
        result = plan_geometry(spec)

        gir = result["gir"]
        features = gir["features"]
        feature_types = [f["type"] for f in features]

        self.assertIn("sketch_profile", feature_types)
        self.assertIn("extrude_intent", feature_types)

    def test_missing_height_uses_default(self) -> None:
        spec = {
            "envelope": {
                "length": {"value": 100, "unit": "mm"},
                "width": {"value": 50, "unit": "mm"},
            },
        }
        result = plan_geometry(spec)
        notices = result["notices"]
        codes = [n["code"] for n in notices]
        self.assertIn("DEFAULT_HEIGHT", codes)

    def test_no_envelope_produces_warning(self) -> None:
        spec = {}
        result = plan_geometry(spec)
        notices = result["notices"]
        codes = [n["code"] for n in notices]
        self.assertIn("NO_ENVELOPE", codes)

    def test_eir_has_create_sketch_and_pad(self) -> None:
        spec = {
            "envelope": {
                "length": {"value": 100, "unit": "mm"},
                "width": {"value": 50, "unit": "mm"},
                "height": {"value": 20, "unit": "mm"},
            },
        }
        result = plan_geometry(spec)
        eir = result["eir"]
        op_types = [op["op_type"] for op in eir["operations"]]
        self.assertIn("create_sketch", op_types)
        self.assertIn("pad", op_types)

    def test_pad_depends_on_sketch(self) -> None:
        spec = {
            "envelope": {
                "length": {"value": 100, "unit": "mm"},
                "width": {"value": 50, "unit": "mm"},
                "height": {"value": 20, "unit": "mm"},
            },
        }
        result = plan_geometry(spec)
        eir = result["eir"]
        ops = {op["id"]: op for op in eir["operations"]}

        pad_ops = [op for op in eir["operations"] if op["op_type"] == "pad"]
        sketch_ops = [op for op in eir["operations"] if op["op_type"] == "create_sketch"]

        self.assertTrue(len(pad_ops) >= 1)
        self.assertTrue(len(sketch_ops) >= 1)

        # Pad should depend on sketch
        pad = pad_ops[0]
        sketch = sketch_ops[0]
        self.assertIn(sketch["id"], pad.get("depends_on", []))


class TestPlanGeometryHoles(unittest.TestCase):
    """Test GIR extraction for hole features."""

    def test_holes_extracted_from_spec(self) -> None:
        spec = {
            "envelope": {
                "length": {"value": 100, "unit": "mm"},
                "width": {"value": 50, "unit": "mm"},
                "height": {"value": 20, "unit": "mm"},
            },
            "geometry": {
                "hole_features": [
                    {
                        "id": "h1",
                        "diameter": {"value": 5, "unit": "mm"},
                        "depth": {"value": 20, "unit": "mm"},
                        "location": {
                            "x": {"value": 40, "unit": "mm"},
                            "y": {"value": 15, "unit": "mm"},
                        },
                    },
                    {
                        "id": "h2",
                        "diameter": {"value": 5, "unit": "mm"},
                        "depth": {"value": 20, "unit": "mm"},
                        "location": {
                            "x": {"value": -40, "unit": "mm"},
                            "y": {"value": 15, "unit": "mm"},
                        },
                    },
                ],
            },
        }
        result = plan_geometry(spec)
        gir = result["gir"]
        hole_features = [f for f in gir["features"] if f["type"] == "hole_intent"]
        self.assertEqual(len(hole_features), 2)
        self.assertEqual(hole_features[0]["diameter"]["value"], 5)

    def test_hole_eir_depends_on_pad(self) -> None:
        spec = {
            "envelope": {
                "length": {"value": 100, "unit": "mm"},
                "width": {"value": 50, "unit": "mm"},
                "height": {"value": 20, "unit": "mm"},
            },
            "geometry": {
                "hole_features": [
                    {
                        "id": "h1",
                        "diameter": {"value": 5, "unit": "mm"},
                        "depth": {"value": 20, "unit": "mm"},
                    },
                ],
            },
        }
        result = plan_geometry(spec)
        eir = result["eir"]
        hole_ops = [op for op in eir["operations"] if op["op_type"] == "hole"]
        self.assertTrue(len(hole_ops) >= 1)
        # Hole should depend on pad
        self.assertTrue(len(hole_ops[0].get("depends_on", [])) > 0)

    def test_missing_diameter_skipped_with_notice(self) -> None:
        spec = {
            "envelope": {"length": {"value": 100, "unit": "mm"}},
            "geometry": {
                "hole_features": [
                    {"id": "bad_hole", "depth": {"value": 10, "unit": "mm"}},
                ],
            },
        }
        result = plan_geometry(spec)
        codes = [n["code"] for n in result["notices"]]
        self.assertIn("MISSING_HOLE_DIAMETER", codes)


class TestPlanGeometryFillets(unittest.TestCase):
    """Test GIR extraction for fillet/chamfer blend features."""

    def test_fillets_extracted(self) -> None:
        spec = {
            "envelope": {
                "length": {"value": 100, "unit": "mm"},
                "width": {"value": 50, "unit": "mm"},
                "height": {"value": 20, "unit": "mm"},
            },
            "geometry": {
                "fillets": [
                    {"radius": {"value": 2, "unit": "mm"}},
                ],
            },
        }
        result = plan_geometry(spec)
        gir = result["gir"]
        blend_features = [f for f in gir["features"] if f["type"] == "blend_intent"]
        self.assertEqual(len(blend_features), 1)
        self.assertEqual(blend_features[0]["blend_type"], "fillet")
        self.assertEqual(blend_features[0]["radius"]["value"], 2)

    def test_fillet_eir_operation(self) -> None:
        spec = {
            "envelope": {
                "length": {"value": 100, "unit": "mm"},
                "width": {"value": 50, "unit": "mm"},
                "height": {"value": 20, "unit": "mm"},
            },
            "geometry": {
                "fillets": [{"radius": {"value": 2, "unit": "mm"}}],
            },
        }
        result = plan_geometry(spec)
        eir = result["eir"]
        fillet_ops = [op for op in eir["operations"] if op["op_type"] == "fillet"]
        self.assertEqual(len(fillet_ops), 1)
        self.assertEqual(fillet_ops[0]["inputs"]["radius"], 2.0)


class TestPlanGeometryDeterminism(unittest.TestCase):
    """Test that planning is deterministic."""

    def test_same_spec_same_hashes(self) -> None:
        spec = {
            "envelope": {
                "length": {"value": 100, "unit": "mm"},
                "width": {"value": 50, "unit": "mm"},
                "height": {"value": 20, "unit": "mm"},
            },
            "geometry": {
                "hole_features": [
                    {
                        "id": "h1",
                        "diameter": {"value": 5, "unit": "mm"},
                        "depth": {"value": 20, "unit": "mm"},
                    },
                ],
                "fillets": [{"radius": {"value": 2, "unit": "mm"}}],
            },
        }
        r1 = plan_geometry(spec)
        r2 = plan_geometry(spec)

        self.assertEqual(r1["metadata"]["gir_hash"], r2["metadata"]["gir_hash"])
        self.assertEqual(r1["metadata"]["eir_hash"], r2["metadata"]["eir_hash"])

    def test_different_spec_different_hashes(self) -> None:
        spec1 = {"envelope": {"length": {"value": 100, "unit": "mm"}}}
        spec2 = {"envelope": {"length": {"value": 200, "unit": "mm"}}}

        r1 = plan_geometry(spec1)
        r2 = plan_geometry(spec2)

        self.assertNotEqual(r1["metadata"]["gir_hash"], r2["metadata"]["gir_hash"])

    def test_metadata_contains_strategy(self) -> None:
        spec = {
            "envelope": {
                "length": {"value": 100, "unit": "mm"},
                "width": {"value": 50, "unit": "mm"},
                "height": {"value": 20, "unit": "mm"},
            },
        }
        result = plan_geometry(spec)
        self.assertIn("strategy", result["metadata"])
        self.assertIn("strategy_confidence", result["metadata"])


class TestSweepLoftEIRGeneration(unittest.TestCase):
    """Test that sweep_intent and loft_intent GIR features produce EIR operations."""

    def test_sweep_intent_produces_sweep_op(self) -> None:
        builder = GIRBuilder()
        frame = builder.add_global_frame()

        profile = builder.add_sketch_profile(
            plane="XY",
            elements=[{"type": "spline", "points": [[0, 0], [1, 1]]}],
            frame_id=frame,
        )
        spine = builder.add_sketch_profile(
            plane="XZ",
            elements=[{"type": "spline", "points": [[0, 0, 0], [10, 0, 0]]}],
            frame_id=frame,
        )
        builder.add_sweep_intent(
            profile_id=profile.id,
            spine_id=spine.id,
            operation_type="add",
            frame_id=frame,
        )

        gir = builder.build()
        eir = _generate_eir(gir, "sweep_based", [])

        op_types = [op.op_type for op in eir.operations]
        self.assertIn("sweep", op_types)

        sweep_op = next(op for op in eir.operations if op.op_type == "sweep")
        self.assertIn("profile_sketch", sweep_op.inputs)
        self.assertIn("spine_sketch", sweep_op.inputs)
        self.assertFalse(sweep_op.inputs["subtractive"])

    def test_sweep_cut_produces_subtractive(self) -> None:
        builder = GIRBuilder()
        frame = builder.add_global_frame()

        profile = builder.add_sketch_profile(
            plane="XY", elements=[], frame_id=frame,
        )
        spine = builder.add_sketch_profile(
            plane="XZ", elements=[], frame_id=frame,
        )
        builder.add_sweep_intent(
            profile_id=profile.id,
            spine_id=spine.id,
            operation_type="cut",
            frame_id=frame,
        )

        gir = builder.build()
        eir = _generate_eir(gir, "sweep_based", [])

        sweep_op = next(op for op in eir.operations if op.op_type == "sweep")
        self.assertTrue(sweep_op.inputs["subtractive"])

    def test_loft_intent_produces_loft_op(self) -> None:
        builder = GIRBuilder()
        frame = builder.add_global_frame()

        s1 = builder.add_sketch_profile(
            plane="XY", elements=[], frame_id=frame,
        )
        s2 = builder.add_sketch_profile(
            plane="XY", elements=[], frame_id=frame,
        )
        builder.add_loft_intent(
            section_ids=[s1.id, s2.id],
            operation_type="add",
            ruled=True,
            closed=False,
            frame_id=frame,
        )

        gir = builder.build()
        eir = _generate_eir(gir, "loft_based", [])

        op_types = [op.op_type for op in eir.operations]
        self.assertIn("loft", op_types)

        loft_op = next(op for op in eir.operations if op.op_type == "loft")
        self.assertTrue(loft_op.inputs["ruled"])
        self.assertFalse(loft_op.inputs["closed"])
        self.assertFalse(loft_op.inputs["subtractive"])

    def test_sweep_depends_on_profile_and_spine_sketches(self) -> None:
        builder = GIRBuilder()
        frame = builder.add_global_frame()

        profile = builder.add_sketch_profile(
            plane="XY", elements=[], frame_id=frame,
        )
        spine = builder.add_sketch_profile(
            plane="XZ", elements=[], frame_id=frame,
        )
        builder.add_sweep_intent(
            profile_id=profile.id,
            spine_id=spine.id,
            operation_type="add",
            frame_id=frame,
        )

        gir = builder.build()
        eir = _generate_eir(gir, "sweep_based", [])

        sweep_op = next(op for op in eir.operations if op.op_type == "sweep")
        sketch_ops = [op for op in eir.operations if op.op_type == "create_sketch"]
        sketch_ids = [op.id for op in sketch_ops]

        # Sweep should depend on both sketch ops
        for sid in sketch_ids:
            self.assertIn(sid, sweep_op.depends_on)

    def test_loft_depends_on_all_section_sketches(self) -> None:
        builder = GIRBuilder()
        frame = builder.add_global_frame()

        s1 = builder.add_sketch_profile(plane="XY", elements=[], frame_id=frame)
        s2 = builder.add_sketch_profile(plane="XY", elements=[], frame_id=frame)
        s3 = builder.add_sketch_profile(plane="XY", elements=[], frame_id=frame)
        builder.add_loft_intent(
            section_ids=[s1.id, s2.id, s3.id],
            operation_type="add",
            frame_id=frame,
        )

        gir = builder.build()
        eir = _generate_eir(gir, "loft_based", [])

        loft_op = next(op for op in eir.operations if op.op_type == "loft")
        sketch_ops = [op for op in eir.operations if op.op_type == "create_sketch"]
        sketch_ids = [op.id for op in sketch_ops]

        for sid in sketch_ids:
            self.assertIn(sid, loft_op.depends_on)


if __name__ == "__main__":
    unittest.main()
