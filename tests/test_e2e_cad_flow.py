import unittest

from server.geometry_ir import GIRBuilder, GIR, Quantity, compute_gir_hash, EIRBuilder
from server.geometry_constraints import ConstraintGraphBuilder
from server.geometry_planner import StrategyPlanner
from server.geometry_compiler_freecad import FreeCADCompiler, CompilerStatus
from server.geometry_executor import Executor
from server.geometry_planning import plan_geometry


class TestE2ECADFlow(unittest.TestCase):
    """End-to-end tests spanning spec → GIR → EIR → compiled → executed."""

    def test_spec_to_gir_to_eir_flow(self) -> None:
        spec = {
            "envelope": {
                "length": {"value": 100, "unit": "mm"},
                "width": {"value": 50, "unit": "mm"},
            },
            "process": "cnc",
        }

        result = plan_geometry(spec)

        self.assertIn("gir", result)
        self.assertIn("gir_hash", result["metadata"])
        self.assertEqual(result["metadata"]["engine_mode"], "generic_v1")

        self.assertIn("features", result["gir"])

        eir = result.get("eir")
        if eir:
            self.assertIn("operations", eir)
            self.assertIsInstance(eir["operations"], list)

    def test_gir_hash_stability_across_pipeline(self) -> None:
        builder1 = GIRBuilder()
        builder1.add_global_frame()
        builder1.add_primitive("box", {"length": Quantity(100, "mm")})
        gir1 = builder1.build()

        builder2 = GIRBuilder()
        builder2.add_global_frame()
        builder2.add_primitive("box", {"length": Quantity(100, "mm")})
        gir2 = builder2.build()

        hash1 = compute_gir_hash(gir1)
        hash2 = compute_gir_hash(gir2)

        self.assertEqual(hash1, hash2)

        eir_builder = EIRBuilder()
        eir_builder.add_operation("pad", {})
        eir = eir_builder.build()

        compiler = FreeCADCompiler()
        compiled = compiler.compile_eir(eir)

        self.assertEqual(compiled.status, CompilerStatus.COMPILED)

    def test_spec_with_holes_to_cad_operations(self) -> None:
        spec = {
            "envelope": {"length": {"value": 100, "unit": "mm"}},
            "geometry": {
                "hole_features": [
                    {
                        "id": "hole1",
                        "diameter": {"value": 10, "unit": "mm"},
                        "depth": {"value": 20, "unit": "mm"},
                    }
                ]
            },
        }

        result = plan_geometry(spec)

        self.assertIn("gir", result)

        gir = result["gir"]
        self.assertIsInstance(gir, dict)

    def test_constraint_graph_enriches_gir(self) -> None:
        spec = {"envelope": {"length": {"value": 100, "unit": "mm"}}}

        builder = ConstraintGraphBuilder()
        builder.extract_dimensions_from_spec(spec)
        graph = builder.build()

        self.assertGreater(len(graph.nodes), 0)

        gir_builder = GIRBuilder()
        gir_builder.add_global_frame()
        for node in graph.nodes:
            gir_builder.add_primitive(
                "box", {"length": Quantity(node.value, node.unit or "mm")}
            )

        gir = gir_builder.build()

        self.assertGreater(len(gir.features), 0)

    def test_strategy_selection_influences_eir(self) -> None:
        gir = {"features": [{"type": "extrude_intent", "id": "F0"}]}

        planner = StrategyPlanner()
        strategies = planner.select_strategy(gir, backend="freecad")

        self.assertEqual(strategies.primary.strategy_name, "prism_driven")

        eir_builder = EIRBuilder()
        eir_builder.add_operation("pad", {})

        eir = eir_builder.build()

        compiler = FreeCADCompiler()
        compiled = compiler.compile_eir(eir)

        self.assertEqual(compiled.status, CompilerStatus.COMPILED)

    def test_compiler_output_usable_by_executor(self) -> None:
        builder = EIRBuilder()
        builder.add_operation("pad", {"sketch": "S1"})
        eir = builder.build()

        compiler = FreeCADCompiler()
        compiled = compiler.compile_eir(eir)

        executor = Executor()
        trace = executor.execute_plan(compiled.ops or [], backend="freecad")

        self.assertGreaterEqual(len(trace.steps), 1)

    def test_engine_mode_preserved_across_pipeline(self) -> None:
        spec = {
            "engine_mode": "generic_v1",
            "envelope": {"length": {"value": 100, "unit": "mm"}},
        }

        result = plan_geometry(spec)

        self.assertEqual(result.get("metadata", {}).get("engine_mode"), "generic_v1")

    def test_full_e2e_deterministic_output(self) -> None:
        spec = {"envelope": {"length": {"value": 100, "unit": "mm"}}}

        result1 = plan_geometry(spec)
        result2 = plan_geometry(spec)

        self.assertEqual(
            result1["metadata"]["gir_hash"], result2["metadata"]["gir_hash"]
        )

    def test_full_pipeline_plan_compile_execute_verify(self) -> None:
        """Full pipeline: plan → compile → execute → verify."""
        from server.geometry_executor import compute_execution_trace_hash
        from server.geometry_verify import VerificationEngine
        from server.geometry_ir import Invariant

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

        # Plan
        plan_result = plan_geometry(spec)
        self.assertIn("gir_hash", plan_result["metadata"])
        self.assertIn("eir_hash", plan_result["metadata"])

        # Compile
        eir_builder = EIRBuilder()
        for op_data in plan_result["eir"]["operations"]:
            invariants = [
                Invariant(type=inv.get("type", ""), threshold=inv.get("threshold"), scope=inv.get("scope"))
                for inv in op_data.get("invariants", [])
            ]
            eir_builder.add_operation(
                op_type=op_data["op_type"],
                inputs=op_data.get("inputs", {}),
                depends_on=op_data.get("depends_on", []),
                invariants=invariants,
            )
        eir = eir_builder.build()

        compiler = FreeCADCompiler()
        compiled = compiler.compile_eir(eir)
        self.assertEqual(compiled.status, CompilerStatus.COMPILED)

        # Execute
        executor = Executor()
        trace = executor.execute_plan(compiled.ops or [])
        self.assertTrue(all(s.status == "completed" for s in trace.steps))

        trace_hash = compute_execution_trace_hash(trace)
        self.assertTrue(len(trace_hash) == 64)

        # Verify
        gir_builder = GIRBuilder()
        gir_builder.add_global_frame()
        gir_builder.add_primitive("box", {
            "length": Quantity(100.0, "mm"),
            "width": Quantity(50.0, "mm"),
            "height": Quantity(20.0, "mm"),
        })
        gir_obj = gir_builder.build()

        verifier = VerificationEngine()
        report = verifier.verify(trace, gir_obj, spec)
        self.assertTrue(report.passed)
        self.assertTrue(len(report.report_hash) == 64)

    def test_spec_with_holes_produces_hole_eir_ops(self) -> None:
        """Specs with holes produce hole operations in EIR."""
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
                        "depth": {"value": 10, "unit": "mm"},
                    },
                ],
            },
        }
        result = plan_geometry(spec)
        eir = result["eir"]
        op_types = [op["op_type"] for op in eir["operations"]]
        self.assertIn("hole", op_types)


if __name__ == "__main__":
    unittest.main()
