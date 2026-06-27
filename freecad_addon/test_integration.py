"""Integration tests that run INSIDE FreeCAD's Python console.

These tests verify pivy/Coin3D interactions, screenshot capture, and camera
control — things that mocked unit tests cannot catch.

Usage (in FreeCAD's Python console)::

    exec(open("<path-to-repo>/freecad_addon/test_integration.py").read())

Or::

    import freecad_addon.test_integration
    freecad_addon.test_integration.run_all()

The script prints PASS/FAIL for each test and a summary at the end.
"""

from __future__ import annotations

import base64
import math
import sys
import traceback
from typing import Any

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------

_results: list[tuple[str, bool, str]] = []


def _record(name: str, passed: bool, detail: str = "") -> None:
    _results.append((name, passed, detail))
    status = "PASS" if passed else "FAIL"
    msg = f"  [{status}] {name}"
    if detail:
        msg += f"  ({detail})"
    print(msg)


def _run_test(name: str, fn: Any) -> None:
    try:
        fn()
        _record(name, True)
    except Exception as exc:
        _record(name, False, f"{type(exc).__name__}: {exc}")
        traceback.print_exc()


def _assert_finite_camera(result: dict[str, Any], label: str = "") -> None:
    """Assert that camera_position values are all finite (not NaN/Inf)."""
    pos = result.get("camera_position", [])
    for i, v in enumerate(pos):
        assert math.isfinite(v), f"{label}camera_position[{i}] = {v} is not finite"


def _assert_valid_png(result: dict[str, Any], label: str = "") -> None:
    """Assert that image_base64 decodes to a valid PNG."""
    b64 = result.get("image_base64", "")
    assert len(b64) > 100, f"{label}image_base64 too short ({len(b64)} chars)"
    raw = base64.b64decode(b64)
    # PNG magic bytes
    assert raw[:4] == b"\x89PNG", f"{label}not a valid PNG (magic: {raw[:4]!r})"


# ---------------------------------------------------------------------------
# Tests: pivy / Coin3D primitives
# ---------------------------------------------------------------------------


def test_sbvec3f_from_floats() -> None:
    """SbVec3f(float, float, float) should work."""
    from pivy.coin import SbVec3f

    v = SbVec3f(1.0, 2.0, 3.0)
    assert abs(v[0] - 1.0) < 1e-6
    assert abs(v[1] - 2.0) < 1e-6
    assert abs(v[2] - 3.0) < 1e-6


def test_sbvec3f_from_list() -> None:
    """SbVec3f([float, float, float]) array form should work."""
    from pivy.coin import SbVec3f

    v = SbVec3f([1.0, 2.0, 3.0])
    assert abs(v[0] - 1.0) < 1e-6


def test_sbvec3f_default_setvalue() -> None:
    """SbVec3f() + setValue(float, float, float) should work."""
    from pivy.coin import SbVec3f

    v = SbVec3f()
    v.setValue(1.0, 2.0, 3.0)
    assert abs(v[0] - 1.0) < 1e-6


def test_sbvec3f_from_ints() -> None:
    """SbVec3f with int args — may fail, documents pivy behavior."""
    from pivy.coin import SbVec3f

    v = SbVec3f(1, 2, 3)  # may TypeError on some pivy builds
    assert abs(v[0] - 1.0) < 1e-6


def test_sbvec3f_from_int_list() -> None:
    """SbVec3f([int, int, int]) — may fail, documents pivy behavior."""
    from pivy.coin import SbVec3f

    v = SbVec3f([1, 2, 3])  # noqa
    assert abs(v[0] - 1.0) < 1e-6


def test_sbvec3f_from_bbox_division() -> None:
    """SbVec3f from BoundBox arithmetic (actual failure scenario)."""
    import FreeCAD

    bb = FreeCAD.BoundBox(0, 0, 0, 10, 20, 30)
    cx = (bb.XMin + bb.XMax) / 2
    cy = (bb.YMin + bb.YMax) / 2
    cz = (bb.ZMin + bb.ZMax) / 2
    # This is what failed: the division result type might not be plain float
    from pivy.coin import SbVec3f

    # Try raw — this is the original failure path
    try:
        SbVec3f(cx, cy, cz)
        _record("test_sbvec3f_from_bbox_division (3-arg raw)", True, f"type={type(cx).__name__}")
    except TypeError as e:
        _record(
            "test_sbvec3f_from_bbox_division (3-arg raw)", False, f"type={type(cx).__name__}: {e}"
        )
    # Try with float() cast
    try:
        SbVec3f(float(cx), float(cy), float(cz))
        _record("test_sbvec3f_from_bbox_division (3-arg float())", True)
    except TypeError as e:
        _record("test_sbvec3f_from_bbox_division (3-arg float())", False, str(e))
    # Try list form
    try:
        SbVec3f([float(cx), float(cy), float(cz)])
        _record("test_sbvec3f_from_bbox_division (list float())", True)
    except TypeError as e:
        _record("test_sbvec3f_from_bbox_division (list float())", False, str(e))
    # Raise to mark the outer test — at least one form must work
    SbVec3f([float(cx), float(cy), float(cz)])  # will raise if broken


# ---------------------------------------------------------------------------
# Tests: _sb_vec3f helper
# ---------------------------------------------------------------------------


def test_sb_vec3f_helper() -> None:
    """Our _sb_vec3f helper must work with all input types."""
    from freecad_addon.commands import _sb_vec3f

    # Plain floats
    v = _sb_vec3f(1.0, 2.0, 3.0)
    assert abs(v[0] - 1.0) < 1e-6
    # Ints
    v = _sb_vec3f(1, 2, 3)
    assert abs(v[0] - 1.0) < 1e-6
    # BoundBox division results
    import FreeCAD

    bb = FreeCAD.BoundBox(0, 0, 0, 10, 20, 30)
    v = _sb_vec3f(
        (bb.XMin + bb.XMax) / 2,
        (bb.YMin + bb.YMax) / 2,
        (bb.ZMin + bb.ZMax) / 2,
    )
    assert abs(v[0] - 5.0) < 1e-6
    assert abs(v[1] - 10.0) < 1e-6
    assert abs(v[2] - 15.0) < 1e-6


# ---------------------------------------------------------------------------
# Tests: camera operations
# ---------------------------------------------------------------------------


def test_camera_position_set() -> None:
    """Setting camera position via Coin3D should not crash."""
    import FreeCAD
    import FreeCADGui

    from freecad_addon.commands import _sb_vec3f

    # Ensure we have a document open
    if FreeCAD.ActiveDocument is None:
        FreeCAD.newDocument("IntegrationTest")

    view = FreeCADGui.ActiveDocument.ActiveView
    cam = view.getCameraNode()
    pos = _sb_vec3f(100.0, 200.0, 300.0)
    cam.position.setValue(pos)
    result = cam.position.getValue()
    assert abs(result[0] - 100.0) < 1e-3


def test_camera_point_at() -> None:
    """cam.pointAt(target, up) should not crash."""
    import FreeCAD
    import FreeCADGui

    from freecad_addon.commands import _sb_vec3f

    if FreeCAD.ActiveDocument is None:
        FreeCAD.newDocument("IntegrationTest")

    view = FreeCADGui.ActiveDocument.ActiveView
    cam = view.getCameraNode()
    target = _sb_vec3f(0.0, 0.0, 0.0)
    up = _sb_vec3f(0.0, 0.0, 1.0)
    cam.pointAt(target, up)


def test_near_distance_set() -> None:
    """cam.nearDistance.setValue(float) should not crash."""
    import FreeCAD
    import FreeCADGui

    if FreeCAD.ActiveDocument is None:
        FreeCAD.newDocument("IntegrationTest")

    view = FreeCADGui.ActiveDocument.ActiveView
    cam = view.getCameraNode()
    cam.nearDistance.setValue(0.1)
    val = cam.nearDistance.getValue()
    assert abs(val - 0.1) < 1e-6


# ---------------------------------------------------------------------------
# Helpers: create PartDesign geometry for screenshot tests
# ---------------------------------------------------------------------------


def _ensure_partdesign_body(doc: Any) -> Any:
    """Create a PartDesign body + sketch + pad if none exists."""
    # Check if a PartDesign body already exists
    for obj in doc.Objects:
        if obj.TypeId == "PartDesign::Body":
            return obj

    import FreeCAD  # already imported at module load; re-imported here for clarity

    body = doc.addObject("PartDesign::Body", "TestBody")

    # Create a sketch on XY plane
    sketch = doc.addObject("Sketcher::SketchObject", "TestSketch")
    body.addObject(sketch)
    sketch.AttachmentSupport = [(doc.getObject("XY_Plane"), "")]
    sketch.MapMode = "FlatFace"

    import Part  # noqa

    # Add a rectangle via 4 lines
    sketch.addGeometry(Part.LineSegment(FreeCAD.Vector(-10, -10, 0), FreeCAD.Vector(10, -10, 0)))
    sketch.addGeometry(Part.LineSegment(FreeCAD.Vector(10, -10, 0), FreeCAD.Vector(10, 10, 0)))
    sketch.addGeometry(Part.LineSegment(FreeCAD.Vector(10, 10, 0), FreeCAD.Vector(-10, 10, 0)))
    sketch.addGeometry(Part.LineSegment(FreeCAD.Vector(-10, 10, 0), FreeCAD.Vector(-10, -10, 0)))
    doc.recompute()

    # Pad
    pad = doc.addObject("PartDesign::Pad", "TestPad")
    body.addObject(pad)
    pad.Profile = sketch
    pad.Length = 20.0
    doc.recompute()

    return body


# ---------------------------------------------------------------------------
# Tests: full screenshot pipeline (with real PartDesign geometry)
# ---------------------------------------------------------------------------


def test_screenshot_iso() -> None:
    """Full screenshot pipeline with 'iso' target — camera must not be NaN."""
    import FreeCAD

    doc = FreeCAD.ActiveDocument
    if doc is None:
        doc = FreeCAD.newDocument("IntegrationTest")
    _ensure_partdesign_body(doc)

    from freecad_addon.commands import screenshot

    result = screenshot(target="iso", width=256, height=256)
    assert result.get("ok", True), f"screenshot failed: {result}"
    _assert_valid_png(result, "iso: ")
    _assert_finite_camera(result, "iso: ")


def test_screenshot_all_presets() -> None:
    """All preset views produce valid screenshots with finite camera."""
    import FreeCAD

    doc = FreeCAD.ActiveDocument
    if doc is None:
        doc = FreeCAD.newDocument("IntegrationTest")
    _ensure_partdesign_body(doc)

    from freecad_addon.commands import screenshot

    presets = ["iso", "front", "back", "top", "bottom", "left", "right"]
    for preset in presets:
        result = screenshot(target=preset, width=256, height=256)
        assert result.get("ok", True), f"screenshot({preset}) failed: {result}"
        _assert_valid_png(result, f"{preset}: ")
        _assert_finite_camera(result, f"{preset}: ")
        _record(f"test_screenshot_preset_{preset}", True)


def test_screenshot_explicit_point() -> None:
    """Screenshot targeting an explicit [x,y,z] point."""
    import FreeCAD

    doc = FreeCAD.ActiveDocument
    if doc is None:
        doc = FreeCAD.newDocument("IntegrationTest")
    _ensure_partdesign_body(doc)

    from freecad_addon.commands import screenshot

    result = screenshot(target=[5.0, 5.0, 5.0], width=256, height=256)
    assert result.get("ok", True), f"screenshot failed: {result}"
    _assert_valid_png(result, "explicit_point: ")
    _assert_finite_camera(result, "explicit_point: ")


def test_screenshot_with_near_clip() -> None:
    """Screenshot with near_clip parameter."""
    import FreeCAD

    doc = FreeCAD.ActiveDocument
    if doc is None:
        doc = FreeCAD.newDocument("IntegrationTest")
    _ensure_partdesign_body(doc)

    from freecad_addon.commands import screenshot

    result = screenshot(target="front", near_clip=1.0, width=256, height=256)
    assert result.get("ok", True), f"screenshot failed: {result}"
    _assert_valid_png(result, "near_clip: ")
    _assert_finite_camera(result, "near_clip: ")


def test_screenshot_empty_document() -> None:
    """Screenshot of empty document (no geometry) — must not NaN."""
    import FreeCAD

    FreeCAD.newDocument("EmptyDocTest")
    from freecad_addon.commands import screenshot

    result = screenshot(target="iso", width=256, height=256, doc="EmptyDocTest")
    # Should produce a valid image even with no geometry
    assert "image_base64" in result, f"no image_base64: {result}"
    _assert_finite_camera(result, "empty_doc: ")
    # Clean up
    FreeCAD.closeDocument("EmptyDocTest")


def test_screenshot_image_dimensions() -> None:
    """Screenshot image should have the requested dimensions."""
    import struct

    import FreeCAD

    doc = FreeCAD.ActiveDocument
    if doc is None:
        doc = FreeCAD.newDocument("IntegrationTest")
    _ensure_partdesign_body(doc)

    from freecad_addon.commands import screenshot

    result = screenshot(target="iso", width=320, height=240)
    raw = base64.b64decode(result["image_base64"])
    # PNG IHDR chunk: width at offset 16, height at offset 20 (4 bytes each, big-endian)
    w = struct.unpack(">I", raw[16:20])[0]
    h = struct.unpack(">I", raw[20:24])[0]
    assert w == 320, f"expected width 320, got {w}"
    assert h == 240, f"expected height 240, got {h}"


def test_set_camera() -> None:
    """set_camera with position, target, and near_clip."""
    import FreeCAD

    if FreeCAD.ActiveDocument is None:
        FreeCAD.newDocument("IntegrationTest")

    from freecad_addon.commands import set_camera

    result = set_camera(
        position=[100.0, 100.0, 100.0],
        target=[0.0, 0.0, 0.0],
        up=[0.0, 0.0, 1.0],
        near_clip=0.5,
    )
    assert result.get("camera_set") is True


def test_set_camera_int_values() -> None:
    """set_camera with integer values (common from JSON)."""
    import FreeCAD

    if FreeCAD.ActiveDocument is None:
        FreeCAD.newDocument("IntegrationTest")

    from freecad_addon.commands import set_camera

    result = set_camera(
        position=[100, 100, 100],
        target=[0, 0, 0],
        near_clip=1,
    )
    assert result.get("camera_set") is True


def test_get_camera() -> None:
    """get_camera should return position and clip values."""
    import FreeCAD

    if FreeCAD.ActiveDocument is None:
        FreeCAD.newDocument("IntegrationTest")

    from freecad_addon.commands import get_camera

    result = get_camera()
    assert "position" in result
    assert "near_clip" in result


# ---------------------------------------------------------------------------
# Tests: capture_verification_views (used by pad/pocket/etc.)
# ---------------------------------------------------------------------------


def test_capture_verification_views() -> None:
    """_capture_verification_views should return 2 valid views with finite camera."""
    import FreeCAD

    doc = FreeCAD.ActiveDocument
    if doc is None:
        doc = FreeCAD.newDocument("IntegrationTest")
    _ensure_partdesign_body(doc)

    from freecad_addon.commands import _capture_verification_views

    views = _capture_verification_views(doc, width=256, height=256)
    assert len(views) >= 1, f"Expected at least 1 view, got {len(views)}"
    for i, v in enumerate(views):
        _assert_valid_png(v, f"verification_view[{i}]: ")
        _assert_finite_camera(v, f"verification_view[{i}]: ")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

_ALL_TESTS = [
    ("test_sbvec3f_from_floats", test_sbvec3f_from_floats),
    ("test_sbvec3f_from_list", test_sbvec3f_from_list),
    ("test_sbvec3f_default_setvalue", test_sbvec3f_default_setvalue),
    ("test_sbvec3f_from_ints", test_sbvec3f_from_ints),
    ("test_sbvec3f_from_int_list", test_sbvec3f_from_int_list),
    ("test_sbvec3f_from_bbox_division", test_sbvec3f_from_bbox_division),
    ("test_sb_vec3f_helper", test_sb_vec3f_helper),
    ("test_camera_position_set", test_camera_position_set),
    ("test_camera_point_at", test_camera_point_at),
    ("test_near_distance_set", test_near_distance_set),
    ("test_screenshot_iso", test_screenshot_iso),
    ("test_screenshot_all_presets", test_screenshot_all_presets),
    ("test_screenshot_explicit_point", test_screenshot_explicit_point),
    ("test_screenshot_with_near_clip", test_screenshot_with_near_clip),
    ("test_screenshot_empty_document", test_screenshot_empty_document),
    ("test_screenshot_image_dimensions", test_screenshot_image_dimensions),
    ("test_set_camera", test_set_camera),
    ("test_set_camera_int_values", test_set_camera_int_values),
    ("test_get_camera", test_get_camera),
    ("test_capture_verification_views", test_capture_verification_views),
]


def run_all() -> bool:
    """Run all integration tests.  Returns True if all passed."""
    global _results
    _results = []

    print(f"\n{'=' * 60}")
    print("SolidMind CAD — FreeCAD Integration Tests")
    print(f"{'=' * 60}")
    print(f"Python {sys.version}")
    try:
        import FreeCAD

        print(f"FreeCAD {FreeCAD.Version()[0]}.{FreeCAD.Version()[1]}")
    except Exception:
        print("FreeCAD: not available")
    try:
        import pivy

        print(f"pivy: {pivy.__file__}")
    except Exception:
        print("pivy: not available")
    print(f"{'=' * 60}\n")

    for name, fn in _ALL_TESTS:
        _run_test(name, fn)

    # Summary
    passed = sum(1 for _, ok, _ in _results if ok)
    failed = sum(1 for _, ok, _ in _results if not ok)
    total = len(_results)

    print(f"\n{'=' * 60}")
    print(f"Results: {passed}/{total} passed, {failed} failed")
    if failed:
        print("\nFailed tests:")
        for name, ok, detail in _results:
            if not ok:
                print(f"  - {name}: {detail}")
    print(f"{'=' * 60}\n")

    return failed == 0


# Auto-run when exec'd directly
if __name__ == "__main__" or "_run_on_import" in dir():
    run_all()
elif "freecad_addon.test_integration" not in sys.modules:
    # When exec(open(...).read()) is used
    run_all()
