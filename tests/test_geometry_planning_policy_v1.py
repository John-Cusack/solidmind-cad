from __future__ import annotations

import unittest

from server.geometry_planning import plan_geometry


class TestGeometryPlanningPolicyV1(unittest.TestCase):
    def test_policy_v1_returns_planning_artifact(self) -> None:
        spec = {
            "meta": {"process": "cnc", "units": "mm"},
            "part": {"envelope": {"x": 100, "y": 60, "z": 20}},
            "geometry": {
                "hole_features": [
                    {
                        "id": "h1",
                        "diameter": {"value": 6, "unit": "mm"},
                        "depth": {"value": 20, "unit": "mm"},
                    }
                ],
                "fillets": [{"radius": {"value": 2, "unit": "mm"}}],
            },
        }
        out = plan_geometry(spec, options={"planning_mode": "policy_v1"})
        self.assertIn("planning_plan", out)
        self.assertIn("planning_plan_hash", out)
        self.assertIn("policy_key", out)
        self.assertIn("question_budget", out)
        self.assertLessEqual(len(out["question_budget"]["questions_asked"]), 2)

    def test_policy_v1_deterministic_hash(self) -> None:
        spec = {
            "meta": {"process": "print_3d", "units": "mm"},
            "part": {"envelope": {"x": 80, "y": 40, "z": 30}},
            "manufacturing": {
                "technology": "fdm",
                "in_house_settings": {
                    "layer_height_mm": 0.2,
                    "nozzle_diameter_mm": 0.4,
                },
            },
            "planning": {"max_overhang_angle_deg": 50, "max_bridge_span_mm": 14},
        }
        r1 = plan_geometry(spec, options={"planning_mode": "policy_v1"})
        r2 = plan_geometry(spec, options={"planning_mode": "policy_v1"})
        self.assertEqual(r1["planning_plan_hash"], r2["planning_plan_hash"])
        self.assertEqual(r1["metadata"]["gir_hash"], r2["metadata"]["gir_hash"])
        self.assertEqual(r1["metadata"]["eir_hash"], r2["metadata"]["eir_hash"])


if __name__ == "__main__":
    unittest.main()
