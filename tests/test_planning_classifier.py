from __future__ import annotations

import unittest

from server.planning_classifier import classify_archetype, resolve_process


class TestPlanningClassifier(unittest.TestCase):
    def test_resolve_process_from_print3d(self) -> None:
        spec = {"process": "print_3d", "manufacturing": {"technology": "fdm"}}
        self.assertEqual(resolve_process(spec), "fdm")

    def test_classify_prismatic_default(self) -> None:
        spec = {"process": "cnc", "geometry": {"hole_features": [{"id": "h1"}]}}
        result = classify_archetype(spec)
        self.assertEqual(result.process, "cnc")
        self.assertEqual(result.archetype, "prismatic")

    def test_classify_revolved_with_features(self) -> None:
        spec = {
            "process": "cnc",
            "geometry": {
                "features": [
                    {"type": "revolve_profile"},
                    {"type": "revolution"},
                ],
            },
        }
        result = classify_archetype(spec)
        self.assertEqual(result.archetype, "revolved")


if __name__ == "__main__":
    unittest.main()
