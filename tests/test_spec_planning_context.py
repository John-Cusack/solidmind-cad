from __future__ import annotations

import unittest

from server.spec_planning_context import normalize_spec_for_planning


class TestSpecPlanningContext(unittest.TestCase):
    def test_finalized_envelope_maps_xyz_to_length_width_height(self) -> None:
        spec = {
            "meta": {"process": "cnc", "units": "mm"},
            "part": {"envelope": {"x": 120, "y": 60, "z": 20}},
        }
        normalized = normalize_spec_for_planning(spec)

        self.assertEqual(normalized["envelope"]["length"]["value"], 120.0)
        self.assertEqual(normalized["envelope"]["width"]["value"], 60.0)
        self.assertEqual(normalized["envelope"]["height"]["value"], 20.0)

    def test_legacy_envelope_preserved_without_none_placeholders(self) -> None:
        spec = {
            "process": "cnc",
            "envelope": {
                "length": {"value": 100, "unit": "mm"},
                "width": {"value": 50, "unit": "mm"},
            },
        }
        normalized = normalize_spec_for_planning(spec)

        self.assertIn("length", normalized["envelope"])
        self.assertIn("width", normalized["envelope"])
        self.assertNotIn("height", normalized["envelope"])


if __name__ == "__main__":
    unittest.main()
