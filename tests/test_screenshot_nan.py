"""Unit tests for screenshot NaN prevention — bbox filtering and Inf/NaN guards.

These tests use mocks so they run without FreeCAD.
"""
from __future__ import annotations

import math
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


class FakeBoundBox:
    """Minimal stand-in for FreeCAD.BoundBox."""

    def __init__(
        self,
        xmin: float = 1e308,
        ymin: float = 1e308,
        zmin: float = 1e308,
        xmax: float = -1e308,
        ymax: float = -1e308,
        zmax: float = -1e308,
    ) -> None:
        self.XMin = xmin
        self.YMin = ymin
        self.ZMin = zmin
        self.XMax = xmax
        self.YMax = ymax
        self.ZMax = zmax

    @property
    def DiagonalLength(self) -> float:
        dx = self.XMax - self.XMin
        dy = self.YMax - self.YMin
        dz = self.ZMax - self.ZMin
        return math.sqrt(dx * dx + dy * dy + dz * dz)

    def add(self, other: FakeBoundBox) -> None:
        self.XMin = min(self.XMin, other.XMin)
        self.YMin = min(self.YMin, other.YMin)
        self.ZMin = min(self.ZMin, other.ZMin)
        self.XMax = max(self.XMax, other.XMax)
        self.YMax = max(self.YMax, other.YMax)
        self.ZMax = max(self.ZMax, other.ZMax)


def _make_obj(bb: FakeBoundBox, null: bool = False) -> SimpleNamespace:
    """Create a mock FreeCAD object with a Shape.BoundBox."""
    shape = SimpleNamespace(BoundBox=bb)
    shape.isNull = lambda: null
    return SimpleNamespace(Shape=shape)


def _make_origin_obj() -> SimpleNamespace:
    """Simulate an Origin axis/plane object with ±1e100 bounds."""
    return _make_obj(FakeBoundBox(-1e100, -1e100, -1e100, 1e100, 1e100, 1e100))


class TestModelBoundingBoxFilter(unittest.TestCase):
    """Tests for _model_bounding_box filtering logic."""

    def _call(self, objects: list[SimpleNamespace]) -> FakeBoundBox:
        """Replicate the filtering logic from commands._model_bounding_box."""
        _MAX_DIAG = 1e10
        combined = FakeBoundBox()
        for obj in objects:
            if not hasattr(obj, "Shape") or obj.Shape is None or obj.Shape.isNull():
                continue
            bb = obj.Shape.BoundBox
            if bb.DiagonalLength > _MAX_DIAG or bb.XMin > bb.XMax:
                continue
            combined.add(bb)
        return combined

    def test_normal_objects_included(self) -> None:
        box = _make_obj(FakeBoundBox(0, 0, 0, 10, 20, 30))
        result = self._call([box])
        self.assertAlmostEqual(result.XMax, 10.0)
        self.assertAlmostEqual(result.YMax, 20.0)
        self.assertAlmostEqual(result.ZMax, 30.0)

    def test_origin_objects_excluded(self) -> None:
        """Origin axes/planes (±1e100) must be skipped."""
        normal = _make_obj(FakeBoundBox(0, 0, 0, 10, 10, 10))
        origin = _make_origin_obj()
        result = self._call([normal, origin])
        # Should only include the normal object's bbox
        self.assertAlmostEqual(result.XMax, 10.0)
        self.assertAlmostEqual(result.DiagonalLength, math.sqrt(300), places=3)

    def test_only_origin_objects_empty(self) -> None:
        """If only Origin objects exist, combined bbox stays empty/default."""
        origin = _make_origin_obj()
        result = self._call([origin])
        # Empty FakeBoundBox has XMin > XMax (1e308 > -1e308)
        self.assertGreater(result.XMin, result.XMax)

    def test_null_shape_excluded(self) -> None:
        null_obj = _make_obj(FakeBoundBox(0, 0, 0, 10, 10, 10), null=True)
        result = self._call([null_obj])
        self.assertGreater(result.XMin, result.XMax)

    def test_no_shape_excluded(self) -> None:
        no_shape = SimpleNamespace()  # no Shape attr
        result = self._call([no_shape])
        self.assertGreater(result.XMin, result.XMax)


class TestCaptureImageNaNGuards(unittest.TestCase):
    """Tests for Inf/NaN guards in _capture_image center/diagonal logic."""

    def _compute_center_and_diagonal(
        self, bbox: FakeBoundBox, target_point: tuple[float, ...] | None = None,
    ) -> tuple[tuple[float, float, float], float]:
        """Replicate the guarded center/diagonal logic from _capture_image."""
        if target_point is None:
            cx = float((bbox.XMin + bbox.XMax) / 2)
            cy = float((bbox.YMin + bbox.YMax) / 2)
            cz = float((bbox.ZMin + bbox.ZMax) / 2)
            if not (math.isfinite(cx) and math.isfinite(cy) and math.isfinite(cz)):
                cx, cy, cz = 0.0, 0.0, 0.0
            center = (cx, cy, cz)
        else:
            center = target_point

        diagonal = bbox.DiagonalLength if bbox.DiagonalLength > 0 else 100.0
        if not math.isfinite(diagonal) or diagonal > 1e10:
            diagonal = 100.0

        return center, diagonal

    def test_normal_bbox(self) -> None:
        bb = FakeBoundBox(0, 0, 0, 10, 20, 30)
        center, diag = self._compute_center_and_diagonal(bb)
        self.assertAlmostEqual(center[0], 5.0)
        self.assertAlmostEqual(center[1], 10.0)
        self.assertAlmostEqual(center[2], 15.0)
        self.assertTrue(math.isfinite(diag))
        self.assertGreater(diag, 0)

    def test_empty_bbox_center_fallback(self) -> None:
        """Empty bbox (XMin=1e308, XMax=-1e308) → center falls back to (0,0,0)."""
        bb = FakeBoundBox()  # default: XMin=1e308, XMax=-1e308
        center, diag = self._compute_center_and_diagonal(bb)
        self.assertEqual(center, (0.0, 0.0, 0.0))
        self.assertAlmostEqual(diag, 100.0)

    def test_huge_bbox_diagonal_fallback(self) -> None:
        """Bbox with >1e10 diagonal → diagonal falls back to 100."""
        bb = FakeBoundBox(-1e10, -1e10, -1e10, 1e10, 1e10, 1e10)
        _, diag = self._compute_center_and_diagonal(bb)
        self.assertAlmostEqual(diag, 100.0)

    def test_inf_bbox_diagonal_fallback(self) -> None:
        """Bbox with Inf diagonal → diagonal falls back to 100."""
        bb = FakeBoundBox(-float("inf"), 0, 0, float("inf"), 10, 10)
        center, diag = self._compute_center_and_diagonal(bb)
        # Center should fall back to 0,0,0 (inf+inf = inf → not finite)
        self.assertEqual(center, (0.0, 0.0, 0.0))
        self.assertAlmostEqual(diag, 100.0)

    def test_target_point_overrides_center(self) -> None:
        """When target_point is provided, bbox center is not used."""
        bb = FakeBoundBox()  # degenerate
        center, _ = self._compute_center_and_diagonal(
            bb, target_point=(1.0, 2.0, 3.0)
        )
        self.assertEqual(center, (1.0, 2.0, 3.0))

    def test_cam_pos_always_finite(self) -> None:
        """Full cam_pos computation must be finite even with degenerate bbox."""
        bb = FakeBoundBox()
        center, diag = self._compute_center_and_diagonal(bb)
        cam_dist = diag * 2.0
        # Iso direction
        dx, dy, dz = 1.0, 1.0, 1.0
        length = math.sqrt(3.0)
        dx, dy, dz = dx / length, dy / length, dz / length
        cam_pos = (
            center[0] + dx * cam_dist,
            center[1] + dy * cam_dist,
            center[2] + dz * cam_dist,
        )
        for i, v in enumerate(cam_pos):
            self.assertTrue(math.isfinite(v), f"cam_pos[{i}] = {v} is not finite")

    def test_origin_objects_dont_cause_nan(self) -> None:
        """End-to-end: Origin objects filtered → finite camera position."""
        # Simulate: _model_bounding_box filters, then _capture_image computes
        _MAX_DIAG = 1e10
        normal = _make_obj(FakeBoundBox(0, 0, 0, 10, 10, 10))
        origin = _make_origin_obj()

        # Filter step
        combined = FakeBoundBox()
        for obj in [normal, origin]:
            bb = obj.Shape.BoundBox
            if bb.DiagonalLength > _MAX_DIAG or bb.XMin > bb.XMax:
                continue
            combined.add(bb)

        # Compute step
        center, diag = self._compute_center_and_diagonal(combined)
        cam_dist = diag * 2.0
        cam_pos = (center[0] + cam_dist, center[1] + cam_dist, center[2] + cam_dist)
        for v in cam_pos:
            self.assertTrue(math.isfinite(v))


if __name__ == "__main__":
    unittest.main()
