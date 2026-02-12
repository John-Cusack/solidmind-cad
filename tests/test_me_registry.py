from __future__ import annotations

import unittest

from server.me_registry import (
    list_archetype_ids,
    list_domain_tags,
    load_archetype_card,
    load_constraint_template,
    load_standards_sources,
)


class TestMERegistry(unittest.TestCase):
    def test_domain_tags_available(self) -> None:
        tags = list_domain_tags()
        self.assertGreaterEqual(len(tags), 5)
        ids = {t.get("id") for t in tags}
        self.assertIn("turbomachinery.turbines.radial", ids)

    def test_archetype_card_loads(self) -> None:
        archetype_ids = list_archetype_ids()
        self.assertIn("turbine_wheel.turbocharger.radial.v1", archetype_ids)

        card = load_archetype_card("turbine_wheel.turbocharger.radial.v1")
        self.assertEqual(card["archetype_id"], "turbine_wheel.turbocharger.radial.v1")

        template = load_constraint_template(card["constraint_template_id"])
        self.assertEqual(template["template_id"], "turbine_wheel.turbocharger.radial.v1")

    def test_standards_policy(self) -> None:
        payload = load_standards_sources()
        self.assertIn("policies", payload)
        self.assertIn("sources", payload)
        self.assertGreaterEqual(len(payload["sources"]), 2)


if __name__ == "__main__":
    unittest.main()
