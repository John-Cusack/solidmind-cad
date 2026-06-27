"""Tests for orchestrator.skeleton."""

from __future__ import annotations

import unittest

from orchestrator.skeleton import (
    build_skeleton_summary,
    check_gate_g2,
    validate_datum_attachment,
    validate_keepout_zones,
    validate_reserved_volumes,
)
from orchestrator.spec import (
    AssemblySkeleton,
    MasterSpec,
    Subsystem,
)


class TestValidateDatumAttachment(unittest.TestCase):
    def test_attached_passes(self) -> None:
        spec = MasterSpec(
            name="test",
            skeleton=AssemblySkeleton(datums={"A": [0, 0, 0]}),
        )
        spec.subsystems.append(
            Subsystem(
                name="gear",
                assembly_constraints={"coaxial_with": "main_shaft"},
            )
        )
        ok, issues = validate_datum_attachment(spec)
        self.assertTrue(ok, issues)

    def test_name_matches_datum(self) -> None:
        spec = MasterSpec(
            name="test",
            skeleton=AssemblySkeleton(datums={"gear": [0, 0, 0]}),
        )
        spec.subsystems.append(Subsystem(name="gear"))
        ok, issues = validate_datum_attachment(spec)
        self.assertTrue(ok, issues)

    def test_unattached_fails(self) -> None:
        spec = MasterSpec(
            name="test",
            skeleton=AssemblySkeleton(datums={"A": [0, 0, 0]}),
        )
        spec.subsystems.append(Subsystem(name="gear"))
        ok, issues = validate_datum_attachment(spec)
        self.assertFalse(ok)
        self.assertTrue(any("gear" in i for i in issues))


class TestValidateReservedVolumes(unittest.TestCase):
    def test_no_overlap_passes(self) -> None:
        spec = MasterSpec(
            name="test",
            skeleton=AssemblySkeleton(
                reserved_volumes={
                    "motor": {"origin": [0, 0, 0], "size": [10, 10, 10]},
                    "gear": {"origin": [20, 0, 0], "size": [10, 10, 10]},
                }
            ),
        )
        ok, issues = validate_reserved_volumes(spec)
        self.assertTrue(ok, issues)

    def test_overlap_detected(self) -> None:
        spec = MasterSpec(
            name="test",
            skeleton=AssemblySkeleton(
                reserved_volumes={
                    "motor": {"origin": [0, 0, 0], "size": [10, 10, 10]},
                    "gear": {"origin": [5, 5, 5], "size": [10, 10, 10]},
                }
            ),
        )
        ok, issues = validate_reserved_volumes(spec)
        self.assertFalse(ok)
        self.assertTrue(any("overlap" in i for i in issues))

    def test_touching_not_overlapping(self) -> None:
        spec = MasterSpec(
            name="test",
            skeleton=AssemblySkeleton(
                reserved_volumes={
                    "a": {"origin": [0, 0, 0], "size": [10, 10, 10]},
                    "b": {"origin": [10, 0, 0], "size": [10, 10, 10]},
                }
            ),
        )
        ok, issues = validate_reserved_volumes(spec)
        self.assertTrue(ok, issues)

    def test_bbox_format(self) -> None:
        spec = MasterSpec(
            name="test",
            skeleton=AssemblySkeleton(
                reserved_volumes={
                    "a": {"bbox": [10, 10, 10], "position": [0, 0, 0]},
                    "b": {"bbox": [10, 10, 10], "position": [5, 5, 5]},
                }
            ),
        )
        ok, issues = validate_reserved_volumes(spec)
        self.assertFalse(ok)

    def test_min_max_format(self) -> None:
        spec = MasterSpec(
            name="test",
            skeleton=AssemblySkeleton(
                reserved_volumes={
                    "a": {"min": [0, 0, 0], "max": [10, 10, 10]},
                    "b": {"min": [20, 0, 0], "max": [30, 10, 10]},
                }
            ),
        )
        ok, issues = validate_reserved_volumes(spec)
        self.assertTrue(ok, issues)


class TestValidateKeepoutZones(unittest.TestCase):
    def test_no_intersection_passes(self) -> None:
        spec = MasterSpec(
            name="test",
            skeleton=AssemblySkeleton(
                reserved_volumes={"motor": {"origin": [0, 0, 0], "size": [10, 10, 10]}},
                keepout_zones=[{"name": "cable", "origin": [50, 0, 0], "size": [5, 5, 50]}],
            ),
        )
        ok, issues = validate_keepout_zones(spec)
        self.assertTrue(ok, issues)

    def test_intersection_fails(self) -> None:
        spec = MasterSpec(
            name="test",
            skeleton=AssemblySkeleton(
                reserved_volumes={"motor": {"origin": [0, 0, 0], "size": [10, 10, 10]}},
                keepout_zones=[{"name": "cable", "origin": [5, 5, 5], "size": [5, 5, 50]}],
            ),
        )
        ok, issues = validate_keepout_zones(spec)
        self.assertFalse(ok)
        self.assertTrue(any("intersects" in i for i in issues))


class TestCheckGateG2(unittest.TestCase):
    def test_complete_passes(self) -> None:
        spec = MasterSpec(
            name="test",
            skeleton=AssemblySkeleton(
                datums={"A": [0, 0, 0]},
                reserved_volumes={"motor": {"origin": [0, 0, 0], "size": [10, 10, 10]}},
            ),
        )
        spec.subsystems.append(
            Subsystem(
                name="gear",
                assembly_constraints={"datum": "A"},
            )
        )
        ok, issues = check_gate_g2(spec)
        self.assertTrue(ok, issues)

    def test_no_datums_fails(self) -> None:
        spec = MasterSpec(name="test")
        ok, issues = check_gate_g2(spec)
        self.assertFalse(ok)
        self.assertTrue(any("datums" in i.lower() for i in issues))

    def test_via_runner(self) -> None:
        """G2 is also callable via runner.check_gate_g2."""
        from orchestrator.runner import check_gate_g2 as runner_g2

        spec = MasterSpec(name="test")
        ok, issues = runner_g2(spec)
        self.assertFalse(ok)


class TestBuildSkeletonSummary(unittest.TestCase):
    def test_summary_keys(self) -> None:
        spec = MasterSpec(
            name="test",
            skeleton=AssemblySkeleton(
                datums={"A": [0, 0, 0], "B": [10, 0, 0]},
                shaft_axes={"main": {}},
                reserved_volumes={"motor": {}},
                keepout_zones=[{"name": "cable"}],
            ),
        )
        spec.subsystems.append(Subsystem(name="gear"))
        summary = build_skeleton_summary(spec)
        self.assertEqual(summary["datum_count"], 2)
        self.assertIn("A", summary["datums"])
        self.assertEqual(summary["subsystem_count"], 1)
        self.assertEqual(len(summary["keepout_zones"]), 1)


if __name__ == "__main__":
    unittest.main()
