from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from typing import Any

from server.cad_gen import CAD_AVAILABLE, ParsedInterface, parse_interface
from server.constants import COVERAGE_THRESHOLDS


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_SENSOR_BRACKET = json.loads(
    (Path(__file__).resolve().parent.parent / "examples" / "cnc" / "sensor_bracket_L2.json")
    .read_text(encoding="utf-8")
)

_PRINT_3D_L2 = json.loads(
    (Path(__file__).resolve().parent.parent / "examples" / "print_3d" / "L2.json")
    .read_text(encoding="utf-8")
)


def _make_finalized_spec(**overrides: Any) -> dict[str, Any]:
    """Return a minimal finalized CNC spec (no _interview/_audit)."""
    spec = copy.deepcopy(_SENSOR_BRACKET)
    for k, v in overrides.items():
        if k in spec:
            if isinstance(spec[k], dict) and isinstance(v, dict):
                spec[k].update(v)
            else:
                spec[k] = v
        else:
            spec[k] = v
    # Ensure it looks finalized (no internal fields)
    spec.pop("_interview", None)
    spec.pop("_audit", None)
    return spec


def _make_finalized_print_3d_spec(**overrides: Any) -> dict[str, Any]:
    """Return a minimal finalized print_3d spec (no _interview/_audit)."""
    spec = copy.deepcopy(_PRINT_3D_L2)
    for k, v in overrides.items():
        if k in spec:
            if isinstance(spec[k], dict) and isinstance(v, dict):
                spec[k].update(v)
            else:
                spec[k] = v
        else:
            spec[k] = v
    spec.pop("_interview", None)
    spec.pop("_audit", None)
    return spec


# ===================================================================
# 1. Interface parser tests — no CadQuery required
# ===================================================================

class TestInterfaceParser(unittest.TestCase):
    """Tests for the regex-based interface string parser."""

    def test_basic_clearance(self) -> None:
        r = parse_interface("2x M3 clearance holes for bolting bracket to base")
        assert r is not None
        self.assertEqual(r.count, 2)
        self.assertEqual(r.thread, "M3")
        self.assertEqual(r.hole_type, "clearance")
        self.assertAlmostEqual(r.diameter_mm, 3.4)
        self.assertIsNone(r.pattern_x)
        self.assertIsNone(r.pattern_y)

    def test_pattern_with_datum(self) -> None:
        r = parse_interface(
            "4x M6 clearance holes on 50x30 pattern, datum A = bottom face"
        )
        assert r is not None
        self.assertEqual(r.count, 4)
        self.assertEqual(r.thread, "M6")
        self.assertEqual(r.hole_type, "clearance")
        self.assertAlmostEqual(r.diameter_mm, 6.6)
        self.assertAlmostEqual(r.pattern_x, 50.0)
        self.assertAlmostEqual(r.pattern_y, 30.0)

    def test_implicit_count(self) -> None:
        r = parse_interface("M10 tapped hole centered on top face")
        assert r is not None
        self.assertEqual(r.count, 1)
        self.assertEqual(r.thread, "M10")
        self.assertEqual(r.hole_type, "tapped")
        self.assertAlmostEqual(r.diameter_mm, 10.0)

    def test_unparseable_returns_none(self) -> None:
        r = parse_interface("Pins into 2x dowel holes on mating part")
        self.assertIsNone(r)

    def test_slot_returns_none(self) -> None:
        r = parse_interface("Slot for cable routing, 5x20mm")
        self.assertIsNone(r)

    def test_decimal_thread(self) -> None:
        r = parse_interface("2x M2.5 through holes")
        assert r is not None
        self.assertEqual(r.count, 2)
        self.assertEqual(r.thread, "M2.5")
        self.assertEqual(r.hole_type, "through")
        self.assertAlmostEqual(r.diameter_mm, 2.9)

    def test_countersink_with_pattern(self) -> None:
        r = parse_interface("3x M3 countersink holes on 25x25 pattern")
        assert r is not None
        self.assertEqual(r.count, 3)
        self.assertEqual(r.thread, "M3")
        self.assertEqual(r.hole_type, "countersink")
        self.assertAlmostEqual(r.diameter_mm, 3.0)
        self.assertAlmostEqual(r.pattern_x, 25.0)
        self.assertAlmostEqual(r.pattern_y, 25.0)

    def test_single_tapped(self) -> None:
        r = parse_interface("1x M8 tapped hole for lifting eye")
        assert r is not None
        self.assertEqual(r.count, 1)
        self.assertEqual(r.thread, "M8")
        self.assertEqual(r.hole_type, "tapped")
        self.assertAlmostEqual(r.diameter_mm, 8.0)

    def test_clearance_without_pattern(self) -> None:
        r = parse_interface("4x M5 clearance holes, 2 on each side")
        assert r is not None
        self.assertEqual(r.count, 4)
        self.assertEqual(r.thread, "M5")
        self.assertEqual(r.hole_type, "clearance")
        self.assertAlmostEqual(r.diameter_mm, 5.5)
        self.assertIsNone(r.pattern_x)
        self.assertIsNone(r.pattern_y)

    def test_unknown_thread_fallback_diameter(self) -> None:
        r = parse_interface("2x M14 clearance holes")
        assert r is not None
        self.assertEqual(r.thread, "M14")
        # M14 not in CLEARANCE_DIAMETERS → 14 + 0.4 = 14.4
        self.assertAlmostEqual(r.diameter_mm, 14.4)

    def test_heat_set_inserts(self) -> None:
        r = parse_interface("2x M3 heat-set inserts for mounting")
        assert r is not None
        self.assertEqual(r.count, 2)
        self.assertEqual(r.thread, "M3")
        self.assertEqual(r.hole_type, "heat-set")
        self.assertAlmostEqual(r.diameter_mm, 4.0)

    def test_press_fit_insert(self) -> None:
        r = parse_interface("M4 press-fit insert")
        assert r is not None
        self.assertEqual(r.count, 1)
        self.assertEqual(r.thread, "M4")
        self.assertEqual(r.hole_type, "press-fit")
        # press-fit uses nominal thread diameter
        self.assertAlmostEqual(r.diameter_mm, 4.0)

    def test_heat_set_plural_inserts(self) -> None:
        r = parse_interface("4x M5 heat-set inserts on 40x30 pattern")
        assert r is not None
        self.assertEqual(r.count, 4)
        self.assertEqual(r.thread, "M5")
        self.assertEqual(r.hole_type, "heat-set")
        self.assertAlmostEqual(r.diameter_mm, 6.4)
        self.assertAlmostEqual(r.pattern_x, 40.0)
        self.assertAlmostEqual(r.pattern_y, 30.0)

    def test_heat_set_unknown_thread_fallback(self) -> None:
        r = parse_interface("2x M14 heat-set inserts")
        assert r is not None
        self.assertEqual(r.thread, "M14")
        # M14 not in HEAT_SET_DIAMETERS → 14 + 1.0 = 15.0
        self.assertAlmostEqual(r.diameter_mm, 15.0)

    def test_singular_insert(self) -> None:
        r = parse_interface("1x M3 heat-set insert for lid")
        assert r is not None
        self.assertEqual(r.count, 1)
        self.assertEqual(r.hole_type, "heat-set")


# ===================================================================
# 1b. BoxCadGenerator format tests — no CadQuery required
# ===================================================================

@unittest.skipUnless(CAD_AVAILABLE, "requires [cad] extra (cadquery)")
class TestBoxCadGeneratorFormats(unittest.TestCase):
    """Verify supported_formats includes freecad."""

    def test_freecad_in_supported_formats(self) -> None:
        from server.cad_gen_box import BoxCadGenerator

        formats = BoxCadGenerator().supported_formats()
        self.assertIn("freecad", formats)
        self.assertIn("step", formats)
        self.assertIn("stl", formats)


# ===================================================================
# 2. CNC CAD generator tests — require CadQuery
# ===================================================================

@unittest.skipUnless(CAD_AVAILABLE, "requires [cad] extra (cadquery)")
class TestCncCadGenerator(unittest.TestCase):
    """Geometry-level tests that import CadQuery."""

    def _generate(self, spec: dict[str, Any], fmt: str = "step", **opts: Any) -> Any:
        from server.cad_gen import generate

        import tempfile
        tmp = Path(tempfile.mkdtemp())
        ext = ".step" if fmt == "step" else ".stl"
        out = tmp / f"test_part{ext}"
        return generate(spec, fmt, out, opts)

    def test_envelope_dimensions(self) -> None:
        import cadquery as cq

        spec = _make_finalized_spec()
        result = self._generate(spec, "step")
        solid = cq.importers.importStep(str(result.file_path))
        bb = solid.val().BoundingBox()
        self.assertAlmostEqual(bb.xlen, 50.0, places=0)
        self.assertAlmostEqual(bb.ylen, 30.0, places=0)
        self.assertAlmostEqual(bb.zlen, 10.0, places=0)

    def test_hole_count(self) -> None:
        import cadquery as cq

        spec = _make_finalized_spec()
        result = self._generate(spec, "step")
        solid = cq.importers.importStep(str(result.file_path))
        cylinders = [f for f in solid.val().Faces() if f.geomType() == "CYLINDER"]
        # 2x M3 clearance holes = 2 cylindrical faces
        self.assertEqual(len(cylinders), 2)

    def test_warning_on_unparseable_interface(self) -> None:
        spec = _make_finalized_spec(
            part={
                **_SENSOR_BRACKET["part"],
                "interfaces": ["Slot for cable routing, 5x20mm"],
            }
        )
        result = self._generate(spec, "step")
        self.assertTrue(any("could not parse" in w for w in result.warnings))

    def test_fillet_from_deburr(self) -> None:
        spec = _make_finalized_spec()
        # process_notes contains "Deburr" → should use 0.3mm fillet
        result = self._generate(spec, "step")
        self.assertTrue(any("0.3mm" in w or "0.3" in w for w in result.warnings
                            if "fillet" in w.lower() or "deburr" in w.lower()))

    def test_stl_export(self) -> None:
        spec = _make_finalized_spec()
        result = self._generate(spec, "stl")
        self.assertTrue(result.file_path.exists())
        self.assertGreater(result.metadata["file_size_bytes"], 0)
        self.assertEqual(result.metadata["format"], "stl")

    def test_warning_output_structure(self) -> None:
        spec = _make_finalized_spec()
        result = self._generate(spec, "step")
        self.assertIsInstance(result.warnings, list)
        for w in result.warnings:
            self.assertIsInstance(w, str)
        self.assertIsInstance(result.metadata, dict)
        self.assertIn("format", result.metadata)
        self.assertIn("feature_count", result.metadata)
        self.assertIn("file_size_bytes", result.metadata)


# ===================================================================
# 2b. 3D-print CAD generator tests — require CadQuery
# ===================================================================

@unittest.skipUnless(CAD_AVAILABLE, "requires [cad] extra (cadquery)")
class TestBoxCadGeneratorPrint3d(unittest.TestCase):
    """Geometry-level tests for print_3d process."""

    def _generate(self, spec: dict[str, Any], fmt: str = "step", **opts: Any) -> Any:
        from server.cad_gen import generate

        import tempfile
        tmp = Path(tempfile.mkdtemp())
        ext = ".step" if fmt == "step" else ".stl"
        out = tmp / f"test_part{ext}"
        return generate(spec, fmt, out, opts)

    def test_envelope_dimensions(self) -> None:
        import cadquery as cq

        spec = _make_finalized_print_3d_spec()
        result = self._generate(spec, "step")
        solid = cq.importers.importStep(str(result.file_path))
        bb = solid.val().BoundingBox()
        self.assertAlmostEqual(bb.xlen, 120.0, places=0)
        self.assertAlmostEqual(bb.ylen, 60.0, places=0)
        # Z will be taller than 20 due to insert bosses
        self.assertGreaterEqual(bb.zlen, 20.0)

    def test_insert_boss_present(self) -> None:
        import cadquery as cq

        spec = _make_finalized_print_3d_spec()
        result = self._generate(spec, "step")
        solid = cq.importers.importStep(str(result.file_path))
        cylinders = [f for f in solid.val().Faces() if f.geomType() == "CYLINDER"]
        # 2x heat-set inserts = boss outer + hole inner per insert = 4 cylinders
        self.assertGreaterEqual(len(cylinders), 2)

    def test_no_fillets_by_default(self) -> None:
        spec = _make_finalized_print_3d_spec()
        result = self._generate(spec, "step")
        # Should NOT have the default fillet warning
        self.assertFalse(
            any("default break-edge" in w for w in result.warnings)
        )

    def test_explicit_fillet_applied(self) -> None:
        spec = _make_finalized_print_3d_spec()
        result = self._generate(spec, "step", fillet_radius=0.5)
        # With explicit fillet, should not have default warning either
        self.assertFalse(
            any("default break-edge" in w for w in result.warnings)
        )

    def test_print_3d_warnings_emitted(self) -> None:
        spec = _make_finalized_print_3d_spec()
        result = self._generate(spec, "step")
        warning_text = " ".join(result.warnings)
        self.assertIn("appearance.color", warning_text)
        self.assertIn("post_processing", warning_text)
        self.assertIn("in_house_settings", warning_text)

    def test_stl_export(self) -> None:
        spec = _make_finalized_print_3d_spec()
        result = self._generate(spec, "stl")
        self.assertTrue(result.file_path.exists())
        self.assertGreater(result.metadata["file_size_bytes"], 0)
        self.assertEqual(result.metadata["format"], "stl")


# ===================================================================
# 3. Tool-level integration tests (spec_generate_cad preconditions)
# ===================================================================

class TestSpecGenerateCadTool(unittest.TestCase):
    """Test precondition checks in spec_generate_cad (no CadQuery needed)."""

    def _call(self, **kwargs: Any) -> dict[str, Any]:
        from server.tools import spec_generate_cad

        return spec_generate_cad(**kwargs)

    def test_not_finalized_interview(self) -> None:
        spec = _make_finalized_spec()
        spec["_interview"] = {"answered": {}}
        result = self._call(spec=spec, output_format="step")
        self.assertTrue(len(result["errors"]) > 0)
        self.assertEqual(result["errors"][0]["code"], "NOT_FINALIZED")

    def test_not_finalized_audit(self) -> None:
        spec = _make_finalized_spec()
        spec["_audit"] = []
        result = self._call(spec=spec, output_format="step")
        self.assertTrue(len(result["errors"]) > 0)
        self.assertEqual(result["errors"][0]["code"], "NOT_FINALIZED")

    def test_insufficient_coverage(self) -> None:
        spec = _make_finalized_spec()
        spec["meta"]["coverage_score"] = 0.10
        spec["meta"]["maturity_level"] = "L2"
        result = self._call(spec=spec, output_format="step")
        self.assertTrue(len(result["errors"]) > 0)
        self.assertEqual(result["errors"][0]["code"], "INSUFFICIENT_COVERAGE")

    def test_hash_mismatch(self) -> None:
        spec = _make_finalized_spec()
        result = self._call(
            spec=spec,
            output_format="step",
            options={"spec_hash": "0000000000000000000000000000000000000000000000000000000000000000"},
        )
        self.assertTrue(len(result["errors"]) > 0)
        self.assertEqual(result["errors"][0]["code"], "HASH_MISMATCH")

    @unittest.skipUnless(CAD_AVAILABLE, "requires [cad] extra (cadquery)")
    def test_print_3d_passes_preconditions(self) -> None:
        spec = _make_finalized_print_3d_spec()
        result = self._call(spec=spec, output_format="step")
        # Should have no precondition errors (coverage 0.9 >= L2 threshold 0.8)
        self.assertEqual(result["errors"], [])


if __name__ == "__main__":
    unittest.main()
