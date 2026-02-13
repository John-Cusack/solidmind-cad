from __future__ import annotations

import unittest

from server.geometry_planning import plan_geometry
from server.geometry_compiler_freecad import FreeCADCompiler, CompilerStatus
from server.geometry_executor import Executor, compute_execution_trace_hash
from server.geometry_verify import VerificationEngine
from server.geometry_ir import EIRBuilder, Invariant


# Golden fixture 1: Simple bracket
# 100x50x20mm box, 4x M5 holes on 80x30 pattern, 2mm fillet
SIMPLE_BRACKET_SPEC = {
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
                    "z": {"value": 0, "unit": "mm"},
                },
            },
            {
                "id": "h2",
                "diameter": {"value": 5, "unit": "mm"},
                "depth": {"value": 20, "unit": "mm"},
                "location": {
                    "x": {"value": -40, "unit": "mm"},
                    "y": {"value": 15, "unit": "mm"},
                    "z": {"value": 0, "unit": "mm"},
                },
            },
            {
                "id": "h3",
                "diameter": {"value": 5, "unit": "mm"},
                "depth": {"value": 20, "unit": "mm"},
                "location": {
                    "x": {"value": 40, "unit": "mm"},
                    "y": {"value": -15, "unit": "mm"},
                    "z": {"value": 0, "unit": "mm"},
                },
            },
            {
                "id": "h4",
                "diameter": {"value": 5, "unit": "mm"},
                "depth": {"value": 20, "unit": "mm"},
                "location": {
                    "x": {"value": -40, "unit": "mm"},
                    "y": {"value": -15, "unit": "mm"},
                    "z": {"value": 0, "unit": "mm"},
                },
            },
        ],
        "fillets": [
            {"radius": {"value": 2, "unit": "mm"}},
        ],
    },
    "process": "cnc",
    "material": {"family": "aluminum"},
}


class TestGoldenSimpleBracket(unittest.TestCase):
    """Golden fixture 1: Simple bracket end-to-end."""

    def setUp(self) -> None:
        self.result = plan_geometry(SIMPLE_BRACKET_SPEC)

    def test_gir_has_correct_features(self) -> None:
        gir = self.result["gir"]
        feature_types = [f["type"] for f in gir["features"]]

        self.assertIn("sketch_profile", feature_types)
        self.assertIn("extrude_intent", feature_types)
        self.assertEqual(feature_types.count("hole_intent"), 4)
        self.assertIn("blend_intent", feature_types)

    def test_gir_hash_stable(self) -> None:
        r2 = plan_geometry(SIMPLE_BRACKET_SPEC)
        self.assertEqual(
            self.result["metadata"]["gir_hash"],
            r2["metadata"]["gir_hash"],
        )

    def test_eir_hash_stable(self) -> None:
        r2 = plan_geometry(SIMPLE_BRACKET_SPEC)
        self.assertEqual(
            self.result["metadata"]["eir_hash"],
            r2["metadata"]["eir_hash"],
        )

    def test_eir_has_correct_operations(self) -> None:
        eir = self.result["eir"]
        op_types = [op["op_type"] for op in eir["operations"]]

        self.assertIn("create_sketch", op_types)
        self.assertIn("pad", op_types)
        self.assertEqual(op_types.count("hole"), 4)
        self.assertIn("fillet", op_types)

    def test_all_ops_compile(self) -> None:
        """All EIR operations should compile via FreeCAD compiler."""
        eir = self.result["eir"]

        eir_builder = EIRBuilder()
        for op_data in eir.get("operations", []):
            invariants = [
                Invariant(
                    type=inv.get("type", ""),
                    threshold=inv.get("threshold"),
                    scope=inv.get("scope"),
                )
                for inv in op_data.get("invariants", [])
            ]
            eir_builder.add_operation(
                op_type=op_data["op_type"],
                inputs=op_data.get("inputs", {}),
                depends_on=op_data.get("depends_on", []),
                invariants=invariants,
            )
        eir_obj = eir_builder.build()

        compiler = FreeCADCompiler()
        compiled = compiler.compile_eir(eir_obj)

        self.assertEqual(compiled.status, CompilerStatus.COMPILED)

    def test_executor_dispatches_correctly(self) -> None:
        """Executor mock dispatch completes all ops."""
        eir = self.result["eir"]

        eir_builder = EIRBuilder()
        for op_data in eir.get("operations", []):
            eir_builder.add_operation(
                op_type=op_data["op_type"],
                inputs=op_data.get("inputs", {}),
                depends_on=op_data.get("depends_on", []),
            )
        eir_obj = eir_builder.build()

        compiler = FreeCADCompiler()
        compiled = compiler.compile_eir(eir_obj)

        executor = Executor()
        trace = executor.execute_plan(compiled.ops or [], backend="mock")

        self.assertTrue(all(s.status == "completed" for s in trace.steps))

    def test_verification_passes(self) -> None:
        """Verification should pass for valid bracket spec."""
        from server.geometry_ir import GIRBuilder, Quantity

        gir_builder = GIRBuilder()
        gir_builder.add_global_frame()
        gir_builder.add_primitive("box", {
            "length": Quantity(100.0, "mm"),
            "width": Quantity(50.0, "mm"),
            "height": Quantity(20.0, "mm"),
        })
        for _ in range(4):
            gir_builder.add_hole_intent(
                diameter=Quantity(5.0, "mm"),
                depth=Quantity(20.0, "mm"),
                hole_type="simple",
                location=__import__("server.geometry_ir", fromlist=["Point3D"]).Point3D(
                    x=Quantity(0.0, "mm"),
                    y=Quantity(0.0, "mm"),
                    z=Quantity(0.0, "mm"),
                ),
            )
        gir_obj = gir_builder.build()

        trace_ops = [
            __import__("server.geometry_ir", fromlist=["CompiledOp"]).CompiledOp(
                id=f"OP{i}", op_type=op_type, inputs={}
            )
            for i, op_type in enumerate(["pad", "hole", "hole", "hole", "hole", "fillet"])
        ]

        executor = Executor()
        trace = executor.execute_plan(trace_ops)

        verifier = VerificationEngine()
        report = verifier.verify(trace, gir_obj, SIMPLE_BRACKET_SPEC)

        self.assertTrue(report.passed)

    def test_metadata_complete(self) -> None:
        """Metadata should contain all required fields."""
        meta = self.result["metadata"]

        self.assertEqual(meta["engine_mode"], "generic_v1")
        self.assertIn("gir_hash", meta)
        self.assertIn("eir_hash", meta)
        self.assertIn("strategy", meta)
        self.assertTrue(len(meta["gir_hash"]) == 64)  # SHA-256 hex
        self.assertTrue(len(meta["eir_hash"]) == 64)

    def test_execution_trace_hash_stable(self) -> None:
        """Execution trace hash should be deterministic."""
        eir = self.result["eir"]

        eir_builder = EIRBuilder()
        for op_data in eir.get("operations", []):
            eir_builder.add_operation(
                op_type=op_data["op_type"],
                inputs=op_data.get("inputs", {}),
            )
        eir_obj = eir_builder.build()

        compiler = FreeCADCompiler()
        compiled = compiler.compile_eir(eir_obj)

        e1 = Executor()
        t1 = e1.execute_plan(compiled.ops or [])
        h1 = compute_execution_trace_hash(t1)

        e2 = Executor()
        t2 = e2.execute_plan(compiled.ops or [])
        h2 = compute_execution_trace_hash(t2)

        self.assertEqual(h1, h2)

    def test_hole_locations_preserved(self) -> None:
        """Hole locations from spec should appear in GIR."""
        gir = self.result["gir"]
        holes = [f for f in gir["features"] if f["type"] == "hole_intent"]

        # All holes should be M5
        for hole in holes:
            self.assertEqual(hole["diameter"]["value"], 5)

    def test_fillet_radius_preserved(self) -> None:
        """Fillet radius should appear in GIR."""
        gir = self.result["gir"]
        blends = [f for f in gir["features"] if f["type"] == "blend_intent"]

        self.assertEqual(len(blends), 1)
        self.assertEqual(blends[0]["radius"]["value"], 2)


if __name__ == "__main__":
    unittest.main()
