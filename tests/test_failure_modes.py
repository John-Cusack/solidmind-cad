"""Tests for the shared part-class failure-mode taxonomy and the ``part_class``
field that dispatches into it.

Covers (a) the loader over ``me_knowledge/failure_modes/`` and the seeded
catalog, (b) tolerant degradation on missing/malformed input, and (c) the
``part_class`` field round-tripping through the design-brief pipeline.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from server.analysis_models import FailureMode, ReflectExpectations
from server.design_models import DesignBrief, PartEntry
from server.design_store import clear as clear_briefs
from server.failure_modes import (
    DEFAULT_TAXONOMY_DIR,
    expectations_for,
    known_part_classes,
    load_taxonomy,
)
from server.tools_design import (
    design_add_interface,
    design_add_part,
    design_get_brief,
    design_save_brief,
    design_update_brief,
    design_update_part,
)

# The classes the catalog ships seeded with (the project-test part classes plus
# the promoted foam-dart launcher classes).
_SEEDED = {
    "hexapod_leg",
    "planetary_gearbox",
    "quadrotor_arm",
    "rc_car_chassis",
    "latch_sear",
    "spring_seat",
    "plunger_rod",
}


class TestTaxonomyLoader(unittest.TestCase):
    def test_default_dir_exists(self) -> None:
        self.assertTrue(DEFAULT_TAXONOMY_DIR.is_dir())

    def test_seeded_classes_present(self) -> None:
        catalog = load_taxonomy()
        self.assertTrue(_SEEDED.issubset(catalog.keys()), f"missing: {_SEEDED - catalog.keys()}")

    def test_entries_are_valid_reflect_expectations(self) -> None:
        for part_class, exp in load_taxonomy().items():
            self.assertIsInstance(exp, ReflectExpectations)
            self.assertEqual(exp.part_class, part_class)
            self.assertGreater(len(exp.failure_modes_to_check), 0, part_class)
            for mode in exp.failure_modes_to_check:
                self.assertIsInstance(mode, FailureMode)
            lo, hi = exp.expected_peak_stress_mpa
            self.assertLess(lo, hi, part_class)
            self.assertTrue(exp.expected_hotspot)

    def test_hexapod_leg_content(self) -> None:
        exp = expectations_for("hexapod_leg")
        self.assertIsNotNone(exp)
        assert exp is not None  # narrow for type-checkers
        self.assertIn(FailureMode.STRESS_CONCENTRATION, exp.failure_modes_to_check)
        self.assertEqual(exp.expected_hotspot, "knee_fillet")

    def test_expectations_for_unknown_returns_none(self) -> None:
        self.assertIsNone(expectations_for("not_a_real_part_class"))

    def test_expectations_for_empty_returns_none(self) -> None:
        self.assertIsNone(expectations_for(""))

    def test_known_part_classes_sorted_superset(self) -> None:
        known = known_part_classes()
        self.assertEqual(known, sorted(known))
        self.assertTrue(_SEEDED.issubset(set(known)))


class TestTaxonomyOverride(unittest.TestCase):
    def test_extra_dir_overrides_and_extends(self) -> None:
        with TemporaryDirectory() as td:
            (Path(td) / "local.yaml").write_text(
                "part_classes:\n"
                "  hexapod_leg:\n"
                "    description: overridden\n"
                "    failure_modes_to_check: [yield]\n"
                "    expected_hotspot: somewhere_else\n"
                "    expected_peak_stress_mpa: [1, 2]\n"
                "  custom_widget:\n"
                "    description: brand new\n"
                "    failure_modes_to_check: [deflection]\n"
                "    expected_hotspot: tip\n"
                "    expected_peak_stress_mpa: [3, 4]\n"
            )
            catalog = load_taxonomy(extra_dirs=[Path(td)])
            # Override wins on a colliding key.
            self.assertEqual(catalog["hexapod_leg"].expected_hotspot, "somewhere_else")
            # New key is added.
            self.assertIn("custom_widget", catalog)
            # Shared keys the override didn't touch survive.
            self.assertIn("latch_sear", catalog)


class TestTaxonomyTolerance(unittest.TestCase):
    def test_missing_dir_returns_empty(self) -> None:
        self.assertEqual(load_taxonomy(extra_dirs=[Path("/nonexistent/xyz")]), load_taxonomy())

    def test_unknown_mode_is_skipped_not_fatal(self) -> None:
        with TemporaryDirectory() as td:
            (Path(td) / "bad.yaml").write_text(
                "part_classes:\n"
                "  partly_bad:\n"
                "    description: has one bad mode\n"
                "    failure_modes_to_check: [yield, not_a_mode]\n"
                "    expected_hotspot: root\n"
                "    expected_peak_stress_mpa: [1, 2]\n"
            )
            exp = load_taxonomy(extra_dirs=[Path(td)]).get("partly_bad")
            self.assertIsNotNone(exp)
            assert exp is not None
            self.assertEqual(exp.failure_modes_to_check, (FailureMode.YIELD,))

    def test_malformed_entry_is_dropped(self) -> None:
        with TemporaryDirectory() as td:
            (Path(td) / "broken.yaml").write_text(
                "part_classes:\n"
                "  no_band:\n"
                "    description: missing the stress band\n"
                "    failure_modes_to_check: [yield]\n"
                "    expected_hotspot: root\n"
            )
            self.assertNotIn("no_band", load_taxonomy(extra_dirs=[Path(td)]))


class TestPartClassField(unittest.TestCase):
    """The part_class field flows through the models and the brief pipeline."""

    def setUp(self) -> None:
        clear_briefs()

    def test_part_entry_round_trip(self) -> None:
        p = PartEntry(name="femur", part_class="hexapod_leg")
        self.assertEqual(PartEntry.from_dict(p.to_dict()).part_class, "hexapod_leg")

    def test_part_entry_default_empty(self) -> None:
        self.assertEqual(PartEntry(name="spacer").part_class, "")
        # Loading a legacy dict without the key must not crash.
        self.assertEqual(PartEntry.from_dict({"name": "spacer"}).part_class, "")

    def test_brief_round_trip(self) -> None:
        b = DesignBrief(brief_id="brief_x", name="Latch", part_class="latch_sear")
        self.assertEqual(DesignBrief.from_dict(b.to_dict()).part_class, "latch_sear")
        self.assertEqual(DesignBrief.from_dict({"brief_id": "b", "name": "n"}).part_class, "")

    def test_save_brief_with_part_class(self) -> None:
        res = design_save_brief(name="Latch", parameters={}, part_class="latch_sear")
        self.assertTrue(res["ok"])
        self.assertEqual(res["brief"]["part_class"], "latch_sear")

    def test_add_part_with_part_class(self) -> None:
        brief = design_save_brief(name="Hexapod", parameters={})["brief"]
        res = design_add_part(brief["brief_id"], name="femur", part_class="hexapod_leg")
        self.assertTrue(res["ok"])
        self.assertEqual(res["part"]["part_class"], "hexapod_leg")

    def test_update_part_sets_part_class(self) -> None:
        brief = design_save_brief(name="Hexapod", parameters={})["brief"]
        design_add_part(brief["brief_id"], name="femur")
        res = design_update_part(brief["brief_id"], "femur", part_class="hexapod_leg")
        self.assertTrue(res["ok"])
        self.assertEqual(res["part"]["part_class"], "hexapod_leg")

    def test_part_class_survives_later_mutations(self) -> None:
        """Adding parts / interfaces / status updates must not wipe part_class."""
        bid = design_save_brief(name="Hexapod", parameters={}, part_class="assembly")["brief"][
            "brief_id"
        ]
        design_add_part(bid, name="femur", part_class="hexapod_leg")
        design_add_part(bid, name="tibia", part_class="hexapod_leg")
        design_add_interface(
            bid, part_a="femur", port_a="knee", part_b="tibia", port_b="knee", spec={}
        )
        design_update_brief(bid, status="sizing")
        brief = design_get_brief(bid)["brief"]
        self.assertEqual(brief["part_class"], "assembly")
        classes = {p["name"]: p["part_class"] for p in brief["parts"]}
        self.assertEqual(classes, {"femur": "hexapod_leg", "tibia": "hexapod_leg"})

    def test_part_class_bridges_to_taxonomy(self) -> None:
        """The end-to-end point: a part's part_class looks up its expectations."""
        brief = design_save_brief(name="Hexapod", parameters={})["brief"]
        part = design_add_part(brief["brief_id"], name="femur", part_class="hexapod_leg")["part"]
        exp = expectations_for(part["part_class"])
        self.assertIsNotNone(exp)
        assert exp is not None
        self.assertEqual(exp.part_class, "hexapod_leg")


if __name__ == "__main__":
    unittest.main()
