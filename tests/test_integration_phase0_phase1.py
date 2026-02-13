import unittest
from server.geometry_ir import GIRBuilder, Quantity, compute_gir_hash, CompilerStatus
from server.geometry_constraints import ConstraintGraphBuilder
from server.geometry_planner import StrategyPlanner
from server.geometry_compiler_freecad import FreeCADCompiler
from server.geometry_planning import plan_geometry


class TestPhase0Phase1Integration(unittest.TestCase):
    def test_full_pipeline(self) -> None:
        spec = {
            "envelope": {"length": {"value": 100}, "width": {"value": 50}},
            "process": "cnc",
        }

        result = plan_geometry(spec)

        self.assertEqual(result["metadata"]["engine_mode"], "generic_v1")
        self.assertIn("gir", result)
        self.assertIn("gir_hash", result["metadata"])

    def test_constraint_graph(self) -> None:
        spec = {"envelope": {"length": {"value": 100}}}

        builder = ConstraintGraphBuilder()
        graph = builder.build_from_spec(spec)

        self.assertGreater(len(graph.nodes), 0)

    def test_gir_builder(self) -> None:
        builder = GIRBuilder()
        builder.add_global_frame()
        builder.add_primitive("box", {"length": Quantity(100, "mm")})

        gir = builder.build()

        self.assertGreater(len(gir.features), 0)

    def test_strategy_planner(self) -> None:
        planner = StrategyPlanner()
        gir = {"features": []}

        strategies = planner.select_strategy(gir, "freecad")

        self.assertIsNotNone(strategies.primary)
        self.assertTrue(len(strategies.primary.strategy_name) > 0)

    def test_compiler(self) -> None:
        compiler = FreeCADCompiler()
        from server.geometry_ir import EIRBuilder, CompiledOp

        eir_builder = EIRBuilder()
        eir_builder.add_operation("pad", {"sketch": "S1"})

        result = compiler.compile_eir(eir_builder.build())

        self.assertIn(result.status, ["compiled", "lowered"])

    def test_gir_hash(self) -> None:
        builder = GIRBuilder()
        builder.add_global_frame()
        builder.add_primitive("box", {"length": Quantity(100, "mm")})
        gir = builder.build()

        hash1 = compute_gir_hash(gir)
        hash2 = compute_gir_hash(gir)

        self.assertEqual(hash1, hash2)


if __name__ == "__main__":
    unittest.main()
