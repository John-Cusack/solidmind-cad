import unittest

from server.geometry_constraints import ConstraintGraphBuilder
from server.geometry_planner import StrategyPlanner


class TestConstraintGraphBuilder(unittest.TestCase):
    def test_build_empty_graph(self) -> None:
        builder = ConstraintGraphBuilder()
        graph = builder.build()

        self.assertEqual(len(graph.nodes), 0)
        self.assertEqual(len(graph.relations), 0)
        self.assertEqual(len(graph.unresolved), 0)

    def test_add_constraint(self) -> None:
        builder = ConstraintGraphBuilder()
        node = builder.add_constraint(
            entity_type="envelope",
            entity_id="overall",
            key="length",
            value=100.0,
            unit="mm",
        )

        self.assertEqual(node.entity_type, "envelope")
        self.assertEqual(node.key, "length")
        self.assertEqual(node.value, 100.0)

    def test_extract_dimensions_from_spec(self) -> None:
        spec = {
            "envelope": {
                "length": {"value": 100, "unit": "mm"},
                "width": {"value": 50, "unit": "mm"},
            }
        }

        builder = ConstraintGraphBuilder()
        builder.extract_dimensions_from_spec(spec)
        graph = builder.build()

        self.assertEqual(len(graph.nodes), 2)

        length_nodes = [n for n in graph.nodes if n.key == "length"]
        self.assertEqual(len(length_nodes), 1)
        self.assertEqual(length_nodes[0].value, 100)

    def test_unit_normalization(self) -> None:
        builder = ConstraintGraphBuilder()
        builder.add_constraint("envelope", "test", "len", 10.0, unit="cm")
        builder.normalize_units(target_unit="mm")

        graph = builder.build()
        self.assertEqual(len(graph.nodes), 1)
        self.assertEqual(graph.nodes[0].value, 100.0)
        self.assertEqual(graph.nodes[0].unit, "mm")

    def test_extract_hole_features(self) -> None:
        spec = {
            "geometry": {
                "hole_features": [
                    {
                        "id": "hole1",
                        "diameter": {"value": 10, "unit": "mm"},
                        "depth": {"value": 20, "unit": "mm"},
                    },
                ]
            }
        }

        builder = ConstraintGraphBuilder()
        builder.extract_feature_constraints(spec)
        graph = builder.build()

        hole_diameters = [n for n in graph.nodes if n.entity_type == "hole" and n.key == "diameter"]
        self.assertEqual(len(hole_diameters), 1)
        self.assertEqual(hole_diameters[0].value, 10)


class TestStrategyPlanner(unittest.TestCase):
    def test_prism_driven_strategy(self) -> None:
        gir = {
            "features": [
                {"type": "extrude_intent", "id": "F0"},
                {"type": "fillet", "id": "F1"},
            ]
        }

        planner = StrategyPlanner()
        strategies = planner.select_strategy(gir, backend="freecad")

        self.assertEqual(strategies.primary.strategy_name, "prism_driven")

    def test_revolve_driven_strategy(self) -> None:
        gir = {
            "features": [
                {"type": "revolve_intent", "id": "F0"},
                {"type": "fillet", "id": "F1"},
            ]
        }

        planner = StrategyPlanner()
        strategies = planner.select_strategy(gir, backend="freecad")

        self.assertEqual(strategies.primary.strategy_name, "revolve_driven")

    def test_hybrid_strategy(self) -> None:
        gir = {
            "features": [
                {"type": "extrude_intent", "id": "F0"},
                {"type": "revolve_intent", "id": "F1"},
            ]
        }

        planner = StrategyPlanner()
        strategies = planner.select_strategy(gir, backend="freecad")

        self.assertIn(
            strategies.primary.strategy_name,
            ["prism_driven", "revolve_driven", "hybrid"],
        )

    def test_basic_box_fallback(self) -> None:
        gir = {"features": []}

        planner = StrategyPlanner()
        strategies = planner.select_strategy(gir, backend="freecad")

        self.assertEqual(strategies.primary.strategy_name, "basic_box")

    def test_strategy_scoring_consistency(self) -> None:
        gir = {
            "features": [
                {"type": "extrude_intent"},
                {"type": "extrude_intent"},
                {"type": "pattern_intent"},
            ]
        }

        planner = StrategyPlanner()
        strategies1 = planner.select_strategy(gir, backend="freecad")
        strategies2 = planner.select_strategy(gir, backend="freecad")

        self.assertEqual(strategies1.primary.score, strategies2.primary.score)


if __name__ == "__main__":
    unittest.main()
