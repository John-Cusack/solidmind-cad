import unittest

from server.geometry_ir import (
    GIR,
    GIRBuilder,
    Quantity,
    compute_gir_hash,
)


class TestGeometryIR(unittest.TestCase):
    def test_primitive_intent_creation(self) -> None:
        builder = GIRBuilder()
        frame_id = builder.add_global_frame()
        dims = {
            "length": Quantity(100.0, "mm"),
            "width": Quantity(50.0, "mm"),
            "height": Quantity(25.0, "mm"),
        }
        primitive = builder.add_primitive("box", dims, frame_id=frame_id)

        self.assertEqual(primitive.id, "F0")
        self.assertEqual(primitive.primitive_type, "box")
        self.assertEqual(primitive.type, "primitive")
        self.assertEqual(primitive.frame_id, frame_id)

    def test_sketch_profile_creation(self) -> None:
        builder = GIRBuilder()
        frame_id = builder.add_global_frame()
        elements = [
            {
                "type": "line",
                "start": {"x": 0, "y": 0, "z": 0},
                "end": {"x": 10, "y": 0, "z": 0},
            },
            {
                "type": "line",
                "start": {"x": 10, "y": 0, "z": 0},
                "end": {"x": 10, "y": 10, "z": 0},
            },
            {
                "type": "line",
                "start": {"x": 10, "y": 10, "z": 0},
                "end": {"x": 0, "y": 10, "z": 0},
            },
            {
                "type": "line",
                "start": {"x": 0, "y": 10, "z": 0},
                "end": {"x": 0, "y": 0, "z": 0},
            },
        ]
        sketch = builder.add_sketch_profile("XY", elements, frame_id=frame_id)

        self.assertEqual(sketch.id, "F0")
        self.assertEqual(sketch.plane, "XY")
        self.assertEqual(len(sketch.elements), 4)

    def test_pattern_intent_creation(self) -> None:
        builder = GIRBuilder()
        frame_id = builder.add_global_frame()
        pattern = builder.add_pattern_intent(
            "polar", ["F0", "F1", "F2"], count=6, frame_id=frame_id
        )

        self.assertEqual(pattern.id, "F0")
        self.assertEqual(pattern.pattern_type, "polar")
        self.assertEqual(pattern.count, 6)
        self.assertEqual(len(pattern.feature_ids), 3)

    def test_gir_build(self) -> None:
        builder = GIRBuilder()
        global_frame = builder.add_global_frame()
        builder.set_metadata("test", "value")

        dims = {"length": Quantity(100.0, "mm"), "width": Quantity(50.0, "mm")}
        builder.add_primitive("box", dims, frame_id=global_frame)

        gir = builder.build()

        self.assertEqual(gir.gir_version, "1.0")
        self.assertEqual(len(gir.frames), 1)
        self.assertEqual(len(gir.features), 1)
        self.assertEqual(gir.metadata["test"], "value")

    def test_deterministic_feature_ids(self) -> None:
        builder1 = GIRBuilder()
        builder1.add_primitive("box", {"length": Quantity(100.0, "mm")})
        builder1.add_primitive(
            "cylinder", {"radius": Quantity(10.0, "mm"), "height": Quantity(20.0, "mm")}
        )

        builder2 = GIRBuilder()
        builder2.add_primitive("box", {"length": Quantity(100.0, "mm")})
        builder2.add_primitive(
            "cylinder", {"radius": Quantity(10.0, "mm"), "height": Quantity(20.0, "mm")}
        )

        features1 = [f.id for f in builder1.build().features]
        features2 = [f.id for f in builder2.build().features]

        self.assertEqual(features1, features2)
        self.assertEqual(features1, ["F0", "F1"])

    def test_sweep_intent_creation(self) -> None:
        builder = GIRBuilder()
        frame_id = builder.add_global_frame()
        profile = builder.add_sketch_profile("XY", [{"type": "circle", "r": 5}], frame_id=frame_id)
        spine = builder.add_sketch_profile(
            "XZ", [{"type": "line", "x1": 0, "y1": 0, "x2": 100, "y2": 0}], frame_id=frame_id
        )

        sweep = builder.add_sweep_intent(
            profile_id=profile.id,
            spine_id=spine.id,
            operation_type="add",
            frame_id=frame_id,
        )

        self.assertEqual(sweep.type, "sweep_intent")
        self.assertEqual(sweep.profile_id, profile.id)
        self.assertEqual(sweep.spine_id, spine.id)
        self.assertEqual(sweep.operation_type, "add")

    def test_loft_intent_creation(self) -> None:
        builder = GIRBuilder()
        frame_id = builder.add_global_frame()
        s1 = builder.add_sketch_profile("XY", [{"type": "circle", "r": 10}], frame_id=frame_id)
        s2 = builder.add_sketch_profile("XY", [{"type": "circle", "r": 5}], frame_id=frame_id)

        loft = builder.add_loft_intent(
            section_ids=[s1.id, s2.id],
            operation_type="add",
            ruled=True,
            frame_id=frame_id,
        )

        self.assertEqual(loft.type, "loft_intent")
        self.assertEqual(loft.section_ids, [s1.id, s2.id])
        self.assertEqual(loft.operation_type, "add")
        self.assertTrue(loft.ruled)
        self.assertFalse(loft.closed)

    def test_sweep_loft_in_gir_build(self) -> None:
        builder = GIRBuilder()
        frame_id = builder.add_global_frame()
        p = builder.add_sketch_profile("XY", [], frame_id=frame_id)
        s = builder.add_sketch_profile("XZ", [], frame_id=frame_id)
        builder.add_sweep_intent(p.id, s.id, "add", frame_id=frame_id)
        builder.add_loft_intent([p.id, s.id], "add", frame_id=frame_id)

        gir = builder.build()
        self.assertEqual(len(gir.features), 4)  # 2 sketches + sweep + loft
        types = [f.type for f in gir.features]
        self.assertIn("sweep_intent", types)
        self.assertIn("loft_intent", types)

    def test_sweep_loft_hash_deterministic(self) -> None:
        def build_gir() -> GIR:
            b = GIRBuilder()
            f = b.add_global_frame()
            p = b.add_sketch_profile("XY", [{"type": "circle", "r": 5}], frame_id=f)
            s = b.add_sketch_profile("XZ", [{"type": "line"}], frame_id=f)
            b.add_sweep_intent(p.id, s.id, "add", frame_id=f)
            return b.build()

        h1 = compute_gir_hash(build_gir())
        h2 = compute_gir_hash(build_gir())
        self.assertEqual(h1, h2)


if __name__ == "__main__":
    unittest.main()
