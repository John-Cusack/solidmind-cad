from __future__ import annotations

import copy
import json
import unittest

import jsonschema

from server.geometry_planning import plan_geometry


class TestPlanningPlanSchema(unittest.TestCase):
    def test_policy_v1_plan_validates_schema(self) -> None:
        spec = {
            "meta": {
                "process": "cnc",
                "units": "mm",
            },
            "part": {
                "envelope": {"x": 100, "y": 50, "z": 20},
            },
            "geometry": {
                "hole_features": [
                    {
                        "id": "h1",
                        "diameter": {"value": 5, "unit": "mm"},
                        "depth": {"value": 20, "unit": "mm"},
                    }
                ],
            },
        }
        result = plan_geometry(spec, options={"planning_mode": "policy_v1"})

        with open("schemas/planning_plan.schema.json") as f:
            schema = json.load(f)

        self.assertIn("planning_plan", result)
        self.assertIn("planning_plan_hash", result)
        self.assertEqual(result["planning_plan"]["policy_key"], result["policy_key"])

        jsonschema.validate(result["planning_plan"], schema)

    def test_policy_v1_plan_rejects_additional_top_level_properties(self) -> None:
        spec = {
            "meta": {
                "process": "cnc",
                "units": "mm",
            },
            "part": {
                "envelope": {"x": 100, "y": 50, "z": 20},
            },
            "geometry": {},
        }
        result = plan_geometry(spec, options={"planning_mode": "policy_v1"})

        with open("schemas/planning_plan.schema.json") as f:
            schema = json.load(f)

        invalid_plan = copy.deepcopy(result["planning_plan"])
        invalid_plan["unexpected"] = True
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(invalid_plan, schema)


if __name__ == "__main__":
    unittest.main()
