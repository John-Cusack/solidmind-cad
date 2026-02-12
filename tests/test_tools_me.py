from __future__ import annotations

import unittest

from server.tools_me import (
    me_build_traceability,
    me_design_loop,
    me_get_archetype_card,
    me_get_knowledge_policy,
    me_instantiate_constraint_sheet,
    me_list_archetypes,
    me_list_domain_tags,
    me_route_request,
    me_validate_constraint_sheet,
)


class TestMETools(unittest.TestCase):
    def test_list_domain_tags(self) -> None:
        out = me_list_domain_tags()
        self.assertTrue(out["ok"])
        self.assertGreater(out["count"], 0)

    def test_list_archetypes(self) -> None:
        out = me_list_archetypes()
        self.assertTrue(out["ok"])
        self.assertIn("turbine_wheel.turbocharger.radial.v1", out["archetype_ids"])

    def test_get_archetype_card(self) -> None:
        out = me_get_archetype_card("turbine_wheel.turbocharger.radial.v1")
        self.assertTrue(out["ok"])
        self.assertEqual(out["archetype_card"]["archetype_id"], "turbine_wheel.turbocharger.radial.v1")

    def test_route_and_validate(self) -> None:
        route = me_route_request("Build me a turbocharger turbine wheel")
        self.assertTrue(route["ok"])

        inst = me_instantiate_constraint_sheet(route["archetype_id"])
        self.assertTrue(inst["ok"])

        report = me_validate_constraint_sheet(inst["constraint_sheet"])
        self.assertTrue(report["ok"])

        trace = me_build_traceability(inst["constraint_sheet"], report)
        self.assertTrue(trace["ok"])

    def test_design_loop(self) -> None:
        out = me_design_loop("Design a radial turbine wheel for a turbocharger")
        self.assertTrue(out["ok"])
        self.assertIn("summary", out)

    def test_knowledge_policy(self) -> None:
        out = me_get_knowledge_policy()
        self.assertTrue(out["ok"])
        self.assertIn("knowledge_policy", out)


if __name__ == "__main__":
    unittest.main()
