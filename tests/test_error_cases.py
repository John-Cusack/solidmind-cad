import unittest

from server.feature_support import load_geometry_capabilities
from server.geometry_compiler_freecad import CompilerStatus, FreeCADCompiler
from server.geometry_constraints import ConstraintGraphBuilder
from server.geometry_executor import Executor
from server.geometry_ir import GIR, EIRBuilder, compute_gir_hash
from server.geometry_planner import StrategyPlanner


class TestErrorCases(unittest.TestCase):
    """Test error handling, edge cases, and invalid inputs."""

    def test_compile_eir_empty_operations(self) -> None:
        eir = EIRBuilder().build()
        compiler = FreeCADCompiler()
        result = compiler.compile_eir(eir)

        self.assertEqual(result.status, CompilerStatus.COMPILED)
        self.assertIsNone(result.ops)

    def test_strategy_planner_with_invalid_backend(self) -> None:
        planner = StrategyPlanner()
        gir = {"features": []}

        strategies = planner.select_strategy(gir, backend="invalid_backend")
        self.assertIn(strategies.primary.strategy_name, ["basic_box"])

    def test_constraint_graph_with_negative_values(self) -> None:
        builder = ConstraintGraphBuilder()
        builder.add_constraint("envelope", "test", "length", -10.0, "mm")
        graph = builder.build()

        self.assertEqual(len(graph.nodes), 1)
        self.assertEqual(graph.nodes[0].value, -10.0)

    def test_gir_hash_with_unexpected_types(self) -> None:
        gir = GIR(
            gir_version="1.0",
            frames=[],
            features=[],
            metadata={"test": "data"},
        )

        hash_val = compute_gir_hash(gir)

        self.assertIsNotNone(hash_val)
        self.assertIsInstance(hash_val, str)

    def test_capability_manifest_missing_backend(self) -> None:
        caps = load_geometry_capabilities()

        self.assertNotIn("backend_not_in_manifest", caps.backends)

    def test_compile_operation_with_empty_inputs(self) -> None:
        builder = EIRBuilder()
        builder.add_operation("pad", {})
        eir = builder.build()

        compiler = FreeCADCompiler()
        result = compiler.compile_eir(eir)

        self.assertEqual(result.status, CompilerStatus.COMPILED)

    def test_strategy_planner_scores_within_bounds(self) -> None:
        planner = StrategyPlanner()
        gir = {"features": [{"type": "extrude_intent"}, {"type": "fillet"}]}

        strategies = planner.select_strategy(gir, "freecad")

        self.assertGreaterEqual(strategies.primary.score, 0)
        self.assertLessEqual(strategies.primary.score, 10.0)

    def test_eir_builder_with_custom_op_type(self) -> None:
        builder = EIRBuilder()
        builder.add_operation("custom_op", {})
        eir = builder.build()

        self.assertEqual(len(eir.operations), 1)
        self.assertEqual(eir.operations[0].op_type, "custom_op")

    def test_executor_with_empty_operations(self) -> None:
        executor = Executor()
        trace = executor.execute_plan([], backend="freecad")

        self.assertEqual(len(trace.steps), 0)


if __name__ == "__main__":
    unittest.main()
