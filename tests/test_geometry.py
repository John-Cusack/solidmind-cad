"""Tests for the solidmind_geometry Rust extension and MCP tool wrappers."""

from __future__ import annotations

import math
import unittest

try:
    import solidmind_geometry as geom

    HAS_GEOM = True
except ImportError:
    HAS_GEOM = False


@unittest.skipUnless(HAS_GEOM, "solidmind_geometry not built")
class TestInvolutePoints(unittest.TestCase):
    def test_returns_correct_count(self):
        pts = geom.involute_points(10.0, 10.0, 15.0, 20)
        self.assertEqual(len(pts), 20)

    def test_points_are_tuples(self):
        pts = geom.involute_points(10.0, 10.0, 15.0, 5)
        for p in pts:
            self.assertIsInstance(p, tuple)
            self.assertEqual(len(p), 2)

    def test_radii_monotonic(self):
        pts = geom.involute_points(10.0, 10.0, 15.0, 20)
        prev_r = 0.0
        for x, y in pts:
            r = math.hypot(x, y)
            self.assertGreaterEqual(r, prev_r - 1e-10)
            prev_r = r


@unittest.skipUnless(HAS_GEOM, "solidmind_geometry not built")
class TestGearParams(unittest.TestCase):
    def test_pitch_diameter(self):
        p = geom.gear_params(2.0, 20)
        self.assertAlmostEqual(p["pitch_diameter"], 40.0, places=10)

    def test_base_diameter(self):
        p = geom.gear_params(2.0, 20)
        expected = 40.0 * math.cos(math.radians(20.0))
        self.assertAlmostEqual(p["base_diameter"], expected, places=10)

    def test_internal_gear(self):
        p = geom.gear_params(2.0, 40, internal=True)
        self.assertLess(p["tip_diameter"], p["pitch_diameter"])
        self.assertGreater(p["root_diameter"], p["pitch_diameter"])


@unittest.skipUnless(HAS_GEOM, "solidmind_geometry not built")
class TestSpurGear(unittest.TestCase):
    def test_returns_elements(self):
        result = geom.spur_gear(1.0, 12)
        self.assertIn("elements", result)
        self.assertIsInstance(result["elements"], list)
        self.assertGreater(len(result["elements"]), 0)

    def test_element_count(self):
        result = geom.spur_gear(1.0, 12)
        # For 12T m=1: rf < rb → 6 elements per tooth (with radial lines)
        self.assertEqual(len(result["elements"]), 12 * 6)

    def test_elements_are_cad_sketch_dicts(self):
        result = geom.spur_gear(1.0, 18)
        for elem in result["elements"]:
            self.assertIsInstance(elem, dict)
            self.assertIn("type", elem)
            self.assertIn(elem["type"], ("spline", "arc", "line", "circle"))

    def test_spline_has_enough_points(self):
        result = geom.spur_gear(1.0, 18, num_involute_pts=20)
        for elem in result["elements"]:
            if elem["type"] == "spline":
                degree = elem["degree"]
                self.assertGreaterEqual(len(elem["points"]), degree + 1)

    def test_has_params(self):
        result = geom.spur_gear(1.25, 18)
        self.assertIn("params", result)
        params = result["params"]
        self.assertIn("pitch_diameter", params)
        self.assertAlmostEqual(params["pitch_diameter"], 1.25 * 18, places=10)

    def test_has_build_hint(self):
        result = geom.spur_gear(1.0, 12)
        self.assertIn("build_hint", result)

    def test_internal_gear(self):
        result = geom.spur_gear(1.0, 40, internal=True)
        self.assertIn("elements", result)
        self.assertEqual(len(result["elements"]), 40 * 4)

    def test_center_offset(self):
        result = geom.spur_gear(1.0, 12, center_x=10.0, center_y=20.0)
        for elem in result["elements"]:
            if elem["type"] == "arc":
                self.assertAlmostEqual(elem["cx"], 10.0, places=10)
                self.assertAlmostEqual(elem["cy"], 20.0, places=10)


@unittest.skipUnless(HAS_GEOM, "solidmind_geometry not built")
class TestToothSlot(unittest.TestCase):
    def test_returns_correct_elements(self):
        result = geom.tooth_slot(1.0, 18)
        # For 18T m=1: rf < rb → 6 elements (with radial lines)
        self.assertEqual(len(result["elements"]), 6)

    def test_has_build_hint(self):
        result = geom.tooth_slot(1.0, 18)
        self.assertIn("build_hint", result)
        self.assertIn("polar_pattern", result["build_hint"])

    def test_has_teeth_count(self):
        result = geom.tooth_slot(1.0, 24)
        self.assertEqual(result["teeth"], 24)


@unittest.skipUnless(HAS_GEOM, "solidmind_geometry not built")
class TestPlanetaryLayout(unittest.TestCase):
    def test_valid_layout(self):
        # sun=18, planet=9, ring=36, 3 planets
        result = geom.planetary_layout(1.0, 18, 9, num_planets=3)
        self.assertIn("sun", result)
        self.assertIn("planet", result)
        self.assertIn("ring", result)
        self.assertIn("planet_positions", result)

    def test_planet_positions_count(self):
        result = geom.planetary_layout(1.0, 18, 9, num_planets=3)
        self.assertEqual(len(result["planet_positions"]), 3)

    def test_sun_has_elements(self):
        result = geom.planetary_layout(1.0, 18, 9, num_planets=3)
        self.assertIn("elements", result["sun"])
        self.assertGreater(len(result["sun"]["elements"]), 0)

    def test_ring_teeth(self):
        result = geom.planetary_layout(1.0, 18, 9, num_planets=3)
        self.assertEqual(result["params"]["ring_teeth"], 36)

    def test_ring_blank_and_tooth_slot(self):
        result = geom.planetary_layout(2.0, 18, 9, num_planets=3)
        self.assertIn("ring_blank", result)
        self.assertIn("ring_tooth_slot", result)
        # ring_blank should have one circle element
        self.assertEqual(len(result["ring_blank"]["elements"]), 1)
        self.assertEqual(result["ring_blank"]["elements"][0]["type"], "circle")
        # ring_tooth_slot should have 4 or 6 elements
        n = len(result["ring_tooth_slot"]["elements"])
        self.assertIn(n, (4, 6))

    def test_ring_blank_radius(self):
        module = 2.0
        result = geom.planetary_layout(module, 18, 9, num_planets=3)
        ring_params = result["params"]["ring"]
        rf = ring_params["root_diameter"] / 2.0
        expected = rf + 1.5 * module
        actual = result["ring_blank"]["elements"][0]["r"]
        self.assertAlmostEqual(actual, expected, places=6)

    def test_assembly_condition_fail(self):
        with self.assertRaises(ValueError):
            geom.planetary_layout(1.0, 18, 10, num_planets=3)

    def test_too_few_planets(self):
        with self.assertRaises(ValueError):
            geom.planetary_layout(1.0, 18, 9, num_planets=1)

    def test_planet_positions_equidistant(self):
        result = geom.planetary_layout(1.0, 18, 9, num_planets=3)
        positions = result["planet_positions"]
        r0 = math.hypot(positions[0][0], positions[0][1])
        for pos in positions[1:]:
            r = math.hypot(pos[0], pos[1])
            self.assertAlmostEqual(r, r0, places=10)


@unittest.skipUnless(HAS_GEOM, "solidmind_geometry not built")
class TestEpicycloidalToothSlot(unittest.TestCase):
    """Test the generalized epicycloidal gear tooth slot."""

    def test_epicycloidal_basic(self):
        result = geom.epicycloidal_tooth_slot(0.1, 8, 80)
        self.assertIn("elements", result)
        self.assertIn("params", result)
        self.assertIn("build_hint", result)
        self.assertEqual(result["teeth"], 8)
        self.assertEqual(len(result["elements"]), 4)

    def test_ogival(self):
        result = geom.epicycloidal_tooth_slot(0.1, 10, 80, profile_type="ogival")
        self.assertEqual(len(result["elements"]), 4)

    def test_modified_involute(self):
        result = geom.epicycloidal_tooth_slot(0.1, 12, 80, profile_type="modified_involute")
        self.assertEqual(len(result["elements"]), 4)

    def test_invalid_teeth_low(self):
        with self.assertRaises(ValueError):
            geom.epicycloidal_tooth_slot(0.1, 3, 80)

    def test_invalid_profile_type(self):
        with self.assertRaises(ValueError):
            geom.epicycloidal_tooth_slot(0.1, 8, 80, profile_type="invalid")

    def test_params_have_diameters(self):
        result = geom.epicycloidal_tooth_slot(0.1, 8, 80)
        p = result["params"]
        self.assertIn("pitch_diameter", p)
        self.assertIn("tip_diameter", p)
        self.assertIn("root_diameter", p)
        self.assertAlmostEqual(p["pitch_diameter"], 0.8, places=6)

    def test_various_tooth_counts(self):
        for teeth in [4, 6, 8, 10, 12, 15, 20, 30]:
            result = geom.epicycloidal_tooth_slot(0.1, teeth, 80)
            self.assertEqual(len(result["elements"]), 4, f"Expected 4 elements for {teeth}T")


@unittest.skipUnless(HAS_GEOM, "solidmind_geometry not built")
class TestSpiral(unittest.TestCase):
    """Test the generalized Archimedean spiral."""

    def test_basic_geometry(self):
        result = geom.spiral_py(2.0, 8.0, 5.0)
        self.assertIn("spiral", result)
        self.assertIn("params", result)
        self.assertIn("build_hint", result)
        p = result["params"]
        self.assertGreater(p["developed_length_mm"], 0)

    def test_with_spring_analysis(self):
        result = geom.spiral_py(
            0.5, 5.0, 12.0,
            strip_thickness_mm=0.05, strip_height_mm=0.15,
            material_e_gpa=210.0, material_yield_mpa=900.0,
        )
        p = result["params"]
        self.assertGreater(p["stiffness_n_m_per_rad"], 0)
        self.assertIn("wall_stress_mpa", p)
        self.assertIn("stress_ok", p)

    def test_with_overcoil(self):
        result = geom.spiral_py(
            0.5, 5.0, 12.0,
            overcoil_angle_deg=270.0, overcoil_style="phillips",
        )
        self.assertIn("overcoil", result)

    def test_with_simple_overcoil(self):
        result = geom.spiral_py(
            0.5, 5.0, 12.0,
            overcoil_angle_deg=180.0, overcoil_style="simple",
        )
        self.assertIn("overcoil", result)

    def test_invalid_radius(self):
        with self.assertRaises(ValueError):
            geom.spiral_py(5.0, 0.5, 12.0)

    def test_invalid_turns(self):
        with self.assertRaises(ValueError):
            geom.spiral_py(0.5, 5.0, 0.1)


@unittest.skipUnless(HAS_GEOM, "solidmind_geometry not built")
class TestSpokePattern(unittest.TestCase):
    """Test the generalized spoke pattern pocket generator."""

    def test_straight(self):
        result = geom.spoke_pattern_py(3.0, 10.0, 12.0, num_spokes=4)
        self.assertIn("elements", result)
        self.assertEqual(len(result["elements"]), 4)
        self.assertEqual(result["num_spokes"], 4)

    def test_styles(self):
        for style in ("straight", "tapered", "curved_s", "curved_c"):
            result = geom.spoke_pattern_py(
                3.0, 10.0, 12.0, spoke_style=style,
            )
            self.assertEqual(len(result["elements"]), 4, f"style {style}")

    def test_various_spoke_counts(self):
        for n in range(2, 13):
            result = geom.spoke_pattern_py(3.0, 10.0, 12.0, num_spokes=n)
            self.assertEqual(len(result["elements"]), 4, f"{n} spokes")

    def test_invalid_spokes_low(self):
        with self.assertRaises(ValueError):
            geom.spoke_pattern_py(3.0, 10.0, 12.0, num_spokes=1)

    def test_invalid_spokes_high(self):
        with self.assertRaises(ValueError):
            geom.spoke_pattern_py(3.0, 10.0, 12.0, num_spokes=13)

    def test_invalid_style(self):
        with self.assertRaises(ValueError):
            geom.spoke_pattern_py(3.0, 10.0, 12.0, spoke_style="invalid")

    def test_weight_reduction(self):
        result = geom.spoke_pattern_py(3.0, 10.0, 12.0, num_spokes=4)
        p = result["params"]
        self.assertIn("weight_reduction_pct", p)
        self.assertGreater(p["weight_reduction_pct"], 0)


class TestRatchetClickProfile(unittest.TestCase):
    """Test the pure-Python ratchet profile generator."""

    def test_basic(self):
        from server.ratchet_profile import ratchet_click_profile

        result = ratchet_click_profile(10.0, 20)
        self.assertEqual(len(result["elements"]), 4)
        self.assertEqual(result["teeth"], 20)
        self.assertIn("build_hint", result)

    def test_custom_angles(self):
        from server.ratchet_profile import ratchet_click_profile

        result = ratchet_click_profile(
            10.0, 24, locking_face_angle_deg=3.0, drive_face_angle_deg=60.0,
        )
        self.assertEqual(result["teeth"], 24)

    def test_invalid_teeth(self):
        from server.ratchet_profile import ratchet_click_profile

        with self.assertRaises(ValueError):
            ratchet_click_profile(10.0, 3)

    def test_invalid_angles(self):
        from server.ratchet_profile import ratchet_click_profile

        with self.assertRaises(ValueError):
            ratchet_click_profile(10.0, 20, locking_face_angle_deg=50.0, drive_face_angle_deg=45.0)


class TestGearTrainSolver(unittest.TestCase):
    """Test the generalized gear train solver."""

    def test_basic_ratio(self):
        from server.gear_train_solver import gear_train_solver

        # Ratio of 60:1 with 3 stages (~3.9:1 per stage)
        result = gear_train_solver(total_ratio=60.0, num_stages=3)
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["stages"]), 3)
        # Ratio error should be small
        self.assertLess(abs(result["ratio_error_pct"]), 0.2)

    def test_2_stage(self):
        from server.gear_train_solver import gear_train_solver

        result = gear_train_solver(total_ratio=10.0, num_stages=2)
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["stages"]), 2)

    def test_has_bore_positions(self):
        from server.gear_train_solver import gear_train_solver

        result = gear_train_solver(total_ratio=60.0, num_stages=3)
        self.assertGreater(len(result["bore_positions"]), 0)

    def test_invalid_stages(self):
        from server.gear_train_solver import gear_train_solver

        with self.assertRaises(ValueError):
            gear_train_solver(total_ratio=10.0, num_stages=1)

    def test_invalid_ratio(self):
        from server.gear_train_solver import gear_train_solver

        with self.assertRaises(ValueError):
            gear_train_solver(total_ratio=-5.0)

    def test_stages_have_all_fields(self):
        from server.gear_train_solver import gear_train_solver

        result = gear_train_solver(total_ratio=60.0, num_stages=3)
        for stage in result["stages"]:
            self.assertIn("wheel_teeth", stage)
            self.assertIn("pinion_teeth", stage)
            self.assertIn("ratio", stage)
            self.assertIn("module", stage)
            self.assertIn("center_distance", stage)


@unittest.skipUnless(HAS_GEOM, "solidmind_geometry not built")
class TestMCPToolWrappers(unittest.TestCase):
    """Test the Python MCP tool wrappers return geometry_ref handles."""

    def setUp(self):
        from server.geometry_store import clear
        clear()

    def tearDown(self):
        from server.geometry_store import clear
        clear()

    def test_spur_gear_tool(self):
        from server.geometry_store import retrieve
        from server.tools_geometry import geometry_spur_gear

        result = geometry_spur_gear(module=1.25, teeth=18)
        self.assertTrue(result["ok"])
        self.assertIn("geometry_ref", result)
        self.assertNotIn("elements", result)
        # For 18T m=1.25: rf < rb → 6 elements per tooth
        self.assertEqual(result["element_count"], 18 * 6)
        # Verify ref resolves to actual elements
        elems = retrieve(result["geometry_ref"])
        self.assertIsNotNone(elems)
        self.assertEqual(len(elems), 18 * 6)

    def test_gear_params_tool(self):
        from server.tools_geometry import geometry_gear_params

        result = geometry_gear_params(module=2.0, teeth=20)
        self.assertTrue(result["ok"])
        self.assertIn("params", result)
        self.assertAlmostEqual(result["params"]["pitch_diameter"], 40.0)

    def test_involute_points_tool(self):
        from server.tools_geometry import geometry_involute_points

        result = geometry_involute_points(10.0, 10.0, 15.0, 20)
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["points"]), 20)

    def test_planetary_layout_tool(self):
        from server.geometry_store import retrieve
        from server.tools_geometry import geometry_planetary_layout

        result = geometry_planetary_layout(module=1.0, sun_teeth=18, planet_teeth=9)
        self.assertTrue(result["ok"])
        self.assertIn("sun", result)
        self.assertIn("planet", result)
        self.assertIn("ring", result)
        # Each gear should have geometry_ref, not elements
        for gear_key in ("sun", "planet", "ring"):
            self.assertIn("geometry_ref", result[gear_key])
            self.assertNotIn("elements", result[gear_key])
            self.assertIn("element_count", result[gear_key])
            elems = retrieve(result[gear_key]["geometry_ref"])
            self.assertIsNotNone(elems)
            self.assertEqual(len(elems), result[gear_key]["element_count"])

    def test_tooth_slot_tool(self):
        from server.geometry_store import retrieve
        from server.tools_geometry import geometry_tooth_slot

        result = geometry_tooth_slot(module=1.0, teeth=18)
        self.assertTrue(result["ok"])
        self.assertIn("geometry_ref", result)
        self.assertNotIn("elements", result)
        # For 18T m=1: rf < rb → 6 elements (with radial lines)
        self.assertEqual(result["element_count"], 6)
        elems = retrieve(result["geometry_ref"])
        self.assertIsNotNone(elems)
        self.assertEqual(len(elems), 6)

    def test_planetary_layout_tool_ring_blank_and_slot(self):
        from server.geometry_store import retrieve
        from server.tools_geometry import geometry_planetary_layout

        result = geometry_planetary_layout(module=2.0, sun_teeth=18, planet_teeth=9)
        self.assertTrue(result["ok"])
        # New ring_blank and ring_tooth_slot keys
        for key in ("ring_blank", "ring_tooth_slot"):
            self.assertIn(key, result)
            self.assertIn("geometry_ref", result[key])
            self.assertNotIn("elements", result[key])
            elems = retrieve(result[key]["geometry_ref"])
            self.assertIsNotNone(elems)
            self.assertEqual(len(elems), result[key]["element_count"])
        # ring_blank should have 1 element (circle)
        self.assertEqual(result["ring_blank"]["element_count"], 1)
        # ring_build_hint and ring_teeth should be present
        self.assertIn("ring_build_hint", result)
        self.assertIn("ring_teeth", result)
        self.assertIn("ring_outer_radius", result)

    def test_epicycloidal_tooth_slot_tool(self):
        from server.geometry_store import retrieve
        from server.tools_geometry import geometry_epicycloidal_tooth_slot

        result = geometry_epicycloidal_tooth_slot(module=0.1, teeth=8, mating_teeth=80)
        self.assertTrue(result["ok"])
        self.assertIn("geometry_ref", result)
        self.assertNotIn("elements", result)
        elems = retrieve(result["geometry_ref"])
        self.assertIsNotNone(elems)
        self.assertEqual(len(elems), result["element_count"])

    def test_spiral_tool(self):
        from server.tools_geometry import geometry_spiral

        result = geometry_spiral(
            inner_radius=0.5, outer_radius=5.0, num_turns=12.0,
        )
        self.assertTrue(result["ok"])
        self.assertIn("spiral_ref", result)

    def test_spiral_tool_with_spring(self):
        from server.tools_geometry import geometry_spiral

        result = geometry_spiral(
            inner_radius=0.5, outer_radius=5.0, num_turns=12.0,
            strip_thickness_mm=0.05, strip_height_mm=0.15,
            material_e_gpa=210.0, material_yield_mpa=900.0,
        )
        self.assertTrue(result["ok"])
        self.assertIn("spiral_ref", result)

    def test_spoke_pattern_tool(self):
        from server.geometry_store import retrieve
        from server.tools_geometry import geometry_spoke_pattern

        result = geometry_spoke_pattern(
            hub_diameter=3.0, rim_inner_diameter=10.0, rim_outer_diameter=12.0,
        )
        self.assertTrue(result["ok"])
        self.assertIn("geometry_ref", result)
        elems = retrieve(result["geometry_ref"])
        self.assertIsNotNone(elems)

    def test_ratchet_tooth_tool(self):
        from server.geometry_store import retrieve
        from server.tools_geometry import geometry_ratchet_tooth

        result = geometry_ratchet_tooth(pitch_diameter=10.0, teeth=20)
        self.assertTrue(result["ok"])
        self.assertIn("geometry_ref", result)
        elems = retrieve(result["geometry_ref"])
        self.assertIsNotNone(elems)
        self.assertEqual(len(elems), 4)

    def test_gear_train_solver_tool(self):
        from server.tools_geometry import geometry_gear_train_solver

        result = geometry_gear_train_solver(total_ratio=60.0)
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["stages"]), 3)
        self.assertIn("bore_positions", result)


###########################################################################
# Phase 1: Lookup-driven tools (pure Python)
###########################################################################


class TestKeywayProfile(unittest.TestCase):
    """Test keyway lookup tables and profile generation."""

    def test_din6885_25mm_shaft(self):
        from server.keyway_data import keyway_profile
        result = keyway_profile(25.0)
        self.assertEqual(len(result["elements"]), 1)
        self.assertIn("build_hint", result)
        self.assertEqual(result["spec"].width, 8)
        self.assertEqual(result["spec"].height, 7)

    def test_din6885_range_boundaries(self):
        from server.keyway_data import keyway_profile
        # 6mm shaft → 2×2 key
        r = keyway_profile(6.0)
        self.assertEqual(r["spec"].width, 2)
        # 50mm shaft → 14×9 key (44-50 range, but 50 is boundary)
        r = keyway_profile(49.0)
        self.assertEqual(r["spec"].width, 14)

    def test_din6885_out_of_range(self):
        from server.keyway_data import keyway_profile
        with self.assertRaises(ValueError):
            keyway_profile(5.0)  # below 6mm

    def test_shaft_depth(self):
        from server.keyway_data import keyway_profile
        r = keyway_profile(25.0)
        # shaft_depth = height/2 = 3.5
        self.assertAlmostEqual(r["spec"].shaft_depth, 3.5)

    def test_woodruff(self):
        from server.keyway_data import keyway_profile
        r = keyway_profile(10.0, standard="woodruff", woodruff_number="404")
        self.assertAlmostEqual(r["spec"].width, 3.18)

    def test_elements_are_rect(self):
        from server.keyway_data import keyway_profile
        r = keyway_profile(25.0)
        self.assertEqual(r["elements"][0]["type"], "rect")

    def test_auto_key_length(self):
        from server.keyway_data import keyway_profile
        r = keyway_profile(25.0)
        # Default key_length = 1.5 × width = 1.5 × 8 = 12
        self.assertAlmostEqual(r["key_length"], 12.0)


class TestORingGroove(unittest.TestCase):
    """Test O-ring lookup and groove computation."""

    def test_dash_210(self):
        from server.oring_data import oring_groove
        result = oring_groove(dash_number=210)
        self.assertAlmostEqual(result["oring"].id_mm, 18.64, places=1)
        self.assertAlmostEqual(result["oring"].cs_mm, 3.53)
        self.assertIn("elements", result)
        self.assertEqual(len(result["elements"]), 1)

    def test_explicit_dimensions(self):
        from server.oring_data import oring_groove
        result = oring_groove(oring_id_mm=20.0, cross_section_mm=3.0)
        self.assertGreater(result["groove_depth"], 0)
        self.assertGreater(result["groove_width"], 0)

    def test_squeeze_range_static(self):
        from server.oring_data import oring_groove
        result = oring_groove(dash_number=210, application="static_radial")
        self.assertGreaterEqual(result["squeeze_pct"], 20)
        self.assertLessEqual(result["squeeze_pct"], 25)

    def test_squeeze_range_dynamic(self):
        from server.oring_data import oring_groove
        result = oring_groove(dash_number=210, application="dynamic_reciprocating")
        self.assertGreaterEqual(result["squeeze_pct"], 10)
        self.assertLessEqual(result["squeeze_pct"], 15)

    def test_gland_fill(self):
        from server.oring_data import oring_groove
        result = oring_groove(dash_number=210)
        # Gland fill should be in reasonable range (60-85%)
        self.assertGreater(result["gland_fill_pct"], 50)
        self.assertLess(result["gland_fill_pct"], 90)

    def test_invalid_dash(self):
        from server.oring_data import oring_groove
        with self.assertRaises(ValueError):
            oring_groove(dash_number=999)

    def test_missing_params(self):
        from server.oring_data import oring_groove
        with self.assertRaises(ValueError):
            oring_groove()  # no dash or explicit dims

    def test_face_groove(self):
        from server.oring_data import oring_groove
        result = oring_groove(dash_number=210, groove_type="face")
        self.assertEqual(len(result["elements"]), 1)

    def test_analysis_string(self):
        from server.oring_data import oring_groove
        result = oring_groove(dash_number=210)
        self.assertIn("AS568-210", result["analysis"])


class TestSectionProperties(unittest.TestCase):
    """Test structural cross-section calculations."""

    def test_rectangle_ixx(self):
        from server.section_properties import compute_section
        r = compute_section("rectangle", width=50, height=100)
        # Ixx = bh³/12 = 50*100³/12 = 4,166,666.67
        self.assertAlmostEqual(r["Ixx"], 50 * 100**3 / 12, places=2)

    def test_rectangle_area(self):
        from server.section_properties import compute_section
        r = compute_section("rectangle", width=50, height=100)
        self.assertAlmostEqual(r["area"], 5000.0)

    def test_circle_ixx(self):
        from server.section_properties import compute_section
        r = compute_section("circle", diameter=100)
        # Ixx = πd⁴/64
        expected = math.pi * 100**4 / 64
        self.assertAlmostEqual(r["Ixx"], expected, places=2)

    def test_circle_symmetry(self):
        from server.section_properties import compute_section
        r = compute_section("circle", diameter=50)
        self.assertAlmostEqual(r["Ixx"], r["Iyy"])

    def test_hollow_circle(self):
        from server.section_properties import compute_section
        r = compute_section("hollow_circle", outer_diameter=100, inner_diameter=80)
        expected = math.pi * (100**4 - 80**4) / 64
        self.assertAlmostEqual(r["Ixx"], expected, places=2)

    def test_hollow_circle_invalid(self):
        from server.section_properties import compute_section
        with self.assertRaises(ValueError):
            compute_section("hollow_circle", outer_diameter=50, inner_diameter=60)

    def test_i_beam(self):
        from server.section_properties import compute_section
        r = compute_section("i_beam", flange_width=100, flange_thickness=10,
                           web_height=200, web_thickness=6)
        self.assertGreater(r["Ixx"], 0)
        self.assertGreater(r["area"], 0)

    def test_c_channel(self):
        from server.section_properties import compute_section
        r = compute_section("c_channel", flange_width=50, flange_thickness=8,
                           web_height=100, web_thickness=6)
        # C-channel has non-zero centroid_x
        self.assertGreater(r["centroid_x"], 0)

    def test_angle_section(self):
        from server.section_properties import compute_section
        r = compute_section("angle", leg1_length=80, leg2_length=80, thickness=8)
        self.assertGreater(r["area"], 0)
        # Non-zero Ixy for angle sections
        self.assertNotAlmostEqual(r["Ixy"], 0.0)

    def test_t_section(self):
        from server.section_properties import compute_section
        r = compute_section("t_section", flange_width=100, flange_thickness=10,
                           web_height=150, web_thickness=8)
        self.assertGreater(r["Ixx"], 0)

    def test_polygon_square(self):
        from server.section_properties import compute_section
        # Square 10×10 centered at origin
        verts = [[-5, -5], [5, -5], [5, 5], [-5, 5]]
        r = compute_section("polygon", vertices=verts)
        self.assertAlmostEqual(r["area"], 100.0, places=2)
        self.assertAlmostEqual(r["Ixx"], 10 * 10**3 / 12, places=1)

    def test_section_modulus(self):
        from server.section_properties import compute_section
        r = compute_section("rectangle", width=50, height=100)
        # Sx = Ixx / ymax = Ixx / 50
        expected_sx = (50 * 100**3 / 12) / 50
        self.assertAlmostEqual(r["Sx"], expected_sx, places=2)

    def test_radius_of_gyration(self):
        from server.section_properties import compute_section
        r = compute_section("rectangle", width=50, height=100)
        # rx = sqrt(Ixx/A)
        expected_rx = math.sqrt((50 * 100**3 / 12) / 5000)
        self.assertAlmostEqual(r["rx"], expected_rx, places=4)

    def test_unknown_shape(self):
        from server.section_properties import compute_section
        with self.assertRaises(ValueError):
            compute_section("hexagon", side=10)


class TestBeltDrive(unittest.TestCase):
    """Test belt drive layout computation."""

    def test_timing_belt_basic(self):
        from server.belt_drive import belt_drive_layout
        result = belt_drive_layout(50.0, 100.0, 200.0, belt_type="timing")
        self.assertIn("belt_length", result)
        self.assertIn("wrap_angle_driver", result)
        self.assertIn("speed_ratio", result)
        self.assertAlmostEqual(result["speed_ratio"], 2.0, places=3)

    def test_belt_length_formula(self):
        from server.belt_drive import belt_drive_layout
        # For equal pulleys: L ≈ 2C + πD
        result = belt_drive_layout(100.0, 100.0, 300.0, belt_type="timing")
        expected = 2 * 300 + math.pi * 100
        self.assertAlmostEqual(result["belt_length"], expected, delta=1.0)

    def test_wrap_angles_equal_pulleys(self):
        from server.belt_drive import belt_drive_layout
        result = belt_drive_layout(100.0, 100.0, 300.0, belt_type="timing")
        self.assertAlmostEqual(result["wrap_angle_driver"], 180.0, delta=0.1)
        self.assertAlmostEqual(result["wrap_angle_driven"], 180.0, delta=0.1)

    def test_timing_teeth(self):
        from server.belt_drive import belt_drive_layout
        result = belt_drive_layout(50.0, 100.0, 200.0, belt_type="timing",
                                   belt_profile="HTD-5M")
        self.assertIn("driver_teeth", result)
        self.assertIn("driven_teeth", result)
        self.assertIn("belt_teeth", result)

    def test_vbelt(self):
        from server.belt_drive import belt_drive_layout
        result = belt_drive_layout(150.0, 300.0, 500.0, belt_type="vbelt",
                                   belt_profile="B")
        self.assertIn("groove_elements", result)
        self.assertIn("build_hint", result)

    def test_pulleys_too_close(self):
        from server.belt_drive import belt_drive_layout
        with self.assertRaises(ValueError):
            belt_drive_layout(100.0, 100.0, 50.0)  # C < (D+d)/2

    def test_invalid_profile(self):
        from server.belt_drive import belt_drive_layout
        with self.assertRaises(ValueError):
            belt_drive_layout(50.0, 100.0, 200.0, belt_type="timing",
                             belt_profile="NONEXISTENT")

    def test_groove_elements_shape(self):
        from server.belt_drive import belt_drive_layout
        result = belt_drive_layout(50.0, 100.0, 200.0, belt_type="timing")
        # Timing tooth profile has 3 lines (trapezoid open at top)
        self.assertEqual(len(result["groove_elements"]), 3)


class TestFourBar(unittest.TestCase):
    """Test four-bar linkage analysis."""

    def test_grashof_crank_rocker(self):
        from server.four_bar import classify_grashof
        # Classic crank-rocker: shortest=input
        result = classify_grashof(ground=4.0, input_link=2.0, coupler=3.0, output_link=3.5)
        self.assertEqual(result, "crank_rocker")

    def test_grashof_double_crank(self):
        from server.four_bar import classify_grashof
        # Shortest = ground → double crank
        result = classify_grashof(ground=2.0, input_link=4.0, coupler=3.0, output_link=3.5)
        self.assertEqual(result, "double_crank")

    def test_non_grashof(self):
        from server.four_bar import classify_grashof
        # s + l > p + q: 3 + 8 = 11 > 5 + 5 = 10
        result = classify_grashof(ground=5.0, input_link=5.0, coupler=3.0, output_link=8.0)
        self.assertEqual(result, "non_grashof_double_rocker")

    def test_solve_position(self):
        from server.four_bar import solve_position
        result = solve_position(4.0, 2.0, 3.0, 3.5, 45.0)
        self.assertIn("output_angle_deg", result)
        self.assertIn("coupler_angle_deg", result)
        self.assertIn("A_x", result)

    def test_transmission_angle(self):
        from server.four_bar import transmission_angle
        mu = transmission_angle(4.0, 2.0, 3.0, 3.5, 45.0)
        self.assertGreater(mu, 0)
        self.assertLess(mu, 180)

    def test_coupler_curve_has_points(self):
        from server.four_bar import coupler_curve
        pts = coupler_curve(4.0, 2.0, 3.0, 3.5, 1.5, 0.5, 100)
        self.assertGreater(len(pts), 0)
        for p in pts:
            self.assertEqual(len(p), 2)

    def test_dead_points(self):
        from server.four_bar import dead_point_detection
        pts = dead_point_detection(4.0, 2.0, 3.0, 3.5)
        # Should find some dead points
        for dp in pts:
            self.assertGreaterEqual(dp, 0)
            self.assertLess(dp, 360.1)

    def test_full_analysis(self):
        from server.four_bar import four_bar_analysis
        result = four_bar_analysis(4.0, 2.0, 3.0, 3.5, 1.5, 0.5)
        self.assertIn("grashof_type", result)
        self.assertIn("elements", result)
        self.assertIn("transmission_angle_range", result)
        self.assertIn("dead_points", result)
        self.assertIn("mechanical_advantage_at_angles", result)
        self.assertIn("build_hint", result)

    def test_impossible_linkage(self):
        from server.four_bar import four_bar_analysis
        with self.assertRaises(ValueError):
            four_bar_analysis(1.0, 1.0, 1.0, 100.0)  # longest > sum of others

    def test_negative_link(self):
        from server.four_bar import classify_grashof
        with self.assertRaises(ValueError):
            classify_grashof(-1.0, 2.0, 3.0, 3.5)


###########################################################################
# MCP tool wrappers for new tools
###########################################################################


class TestNewMCPToolWrappers(unittest.TestCase):
    """Test Python MCP tool wrappers for the 10 new geometry tools."""

    def setUp(self):
        from server.geometry_store import clear
        clear()

    def tearDown(self):
        from server.geometry_store import clear
        clear()

    def test_keyway_profile_tool(self):
        from server.geometry_store import retrieve
        from server.tools_geometry import geometry_keyway_profile

        result = geometry_keyway_profile(shaft_diameter=25.0)
        self.assertTrue(result["ok"])
        self.assertIn("geometry_ref", result)
        self.assertEqual(result["element_count"], 1)
        self.assertEqual(result["key_width"], 8)
        self.assertEqual(result["key_height"], 7)
        elems = retrieve(result["geometry_ref"])
        self.assertIsNotNone(elems)

    def test_oring_groove_tool(self):
        from server.geometry_store import retrieve
        from server.tools_geometry import geometry_oring_groove

        result = geometry_oring_groove(dash_number=210)
        self.assertTrue(result["ok"])
        self.assertIn("geometry_ref", result)
        self.assertIn("groove_depth", result)
        self.assertIn("squeeze_pct", result)
        elems = retrieve(result["geometry_ref"])
        self.assertIsNotNone(elems)

    def test_section_properties_tool(self):
        from server.tools_geometry import geometry_section_properties

        result = geometry_section_properties(shape="rectangle", width=50, height=100)
        self.assertTrue(result["ok"])
        self.assertIn("Ixx", result)
        self.assertAlmostEqual(result["area"], 5000.0)

    def test_belt_drive_tool(self):
        from server.geometry_store import retrieve
        from server.tools_geometry import geometry_belt_drive

        result = geometry_belt_drive(
            driver_diameter=50.0, driven_diameter=100.0, center_distance=200.0,
        )
        self.assertTrue(result["ok"])
        self.assertIn("belt_length", result)
        self.assertIn("speed_ratio", result)
        # Timing belt should have groove ref
        self.assertIn("geometry_ref", result)
        elems = retrieve(result["geometry_ref"])
        self.assertIsNotNone(elems)

    def test_four_bar_tool(self):
        from server.geometry_store import retrieve
        from server.tools_geometry import geometry_four_bar

        result = geometry_four_bar(
            ground_length=4.0, input_length=2.0,
            coupler_length=3.0, output_length=3.5,
            coupler_point_x=1.5, coupler_point_y=0.5,
        )
        self.assertTrue(result["ok"])
        self.assertIn("geometry_ref", result)
        self.assertIn("grashof_type", result)
        self.assertIn("transmission_angle_range", result)
        elems = retrieve(result["geometry_ref"])
        self.assertIsNotNone(elems)


###########################################################################
# Rust-level tests for Phase 2 + 3 tools
###########################################################################


@unittest.skipUnless(HAS_GEOM, "solidmind_geometry not built")
class TestBevelGear(unittest.TestCase):
    """Tests for the bevel gear Rust function."""

    def test_basic_result_keys(self):
        r = geom.bevel_gear(2.0, 20, 30, 20.0, 90.0, 0.0, 0.0, 0.0, 30)
        self.assertIn("elements", r)
        self.assertIn("params", r)
        self.assertIn("teeth", r)
        self.assertIn("build_hint", r)

    def test_pitch_diameter(self):
        r = geom.bevel_gear(2.0, 20, 30, 20.0, 90.0, 0.0, 0.0, 0.0, 30)
        self.assertAlmostEqual(r["params"]["pitch_diameter"], 2.0 * 20, places=3)

    def test_pitch_cone_angle(self):
        """For 90° shaft angle, pitch cone angles should sum to 90°."""
        r = geom.bevel_gear(2.0, 20, 30, 20.0, 90.0, 0.0, 0.0, 0.0, 30)
        delta = r["params"]["pitch_cone_angle_deg"]
        mate_delta = math.degrees(math.atan(30.0 / 20.0))
        self.assertAlmostEqual(delta + mate_delta, 90.0, places=3)

    def test_virtual_teeth_tredgold(self):
        """Virtual teeth = N / cos(pitch_cone_angle)."""
        r = geom.bevel_gear(2.0, 20, 30, 20.0, 90.0, 0.0, 0.0, 0.0, 30)
        delta_rad = math.radians(r["params"]["pitch_cone_angle_deg"])
        expected = 20 / math.cos(delta_rad)
        self.assertAlmostEqual(r["params"]["virtual_teeth"], expected, places=3)

    def test_elements_not_empty(self):
        r = geom.bevel_gear(2.0, 20, 30, 20.0, 90.0, 0.0, 0.0, 0.0, 30)
        self.assertGreater(len(r["elements"]), 0)

    def test_center_offset(self):
        r = geom.bevel_gear(2.0, 20, 30, 20.0, 90.0, 0.0, 50.0, 50.0, 30)
        # Elements should be offset near (50, 50)
        has_offset = False
        for el in r["elements"]:
            if el["type"] == "spline":
                for pt in el["points"]:
                    if abs(pt[0] - 50) < 30 and abs(pt[1] - 50) < 30:
                        has_offset = True
                        break
        self.assertTrue(has_offset)


@unittest.skipUnless(HAS_GEOM, "solidmind_geometry not built")
class TestWormGear(unittest.TestCase):
    """Tests for the worm gear Rust function."""

    def test_basic_result_keys(self):
        r = geom.worm_gear(2.0, 1, 40, 20.0, 0.0, 0.0, 0.0, 30)
        self.assertIn("worm_thread_elements", r)
        self.assertIn("wheel_profile_elements", r)
        self.assertIn("params", r)
        self.assertIn("build_hint", r)

    def test_wheel_pitch_diameter(self):
        r = geom.worm_gear(2.0, 1, 40, 20.0, 0.0, 0.0, 0.0, 30)
        self.assertAlmostEqual(r["params"]["wheel_pitch_diameter"], 2.0 * 40, places=3)

    def test_gear_ratio(self):
        """Gear ratio = wheel_teeth / worm_starts."""
        r = geom.worm_gear(2.0, 2, 40, 20.0, 0.0, 0.0, 0.0, 30)
        ratio = r["params"]["wheel_teeth"] / r["params"]["worm_starts"]
        self.assertEqual(ratio, 20.0)

    def test_self_locking(self):
        """Single-start worm with small lead angle should be close to self-locking threshold."""
        r = geom.worm_gear(2.0, 1, 40, 20.0, 0.0, 0.0, 0.0, 30)
        # Lead angle should be small for 1-start worm
        self.assertLess(r["params"]["lead_angle_deg"], 15.0)

    def test_efficiency_range(self):
        r = geom.worm_gear(2.0, 1, 40, 20.0, 0.0, 0.0, 0.0, 30)
        self.assertGreater(r["params"]["efficiency"], 0.0)
        self.assertLess(r["params"]["efficiency"], 1.0)

    def test_worm_thread_is_trapezoidal(self):
        """Worm thread cross-section should be 4 lines (trapezoid)."""
        r = geom.worm_gear(2.0, 1, 40, 20.0, 0.0, 0.0, 0.0, 30)
        lines = [e for e in r["worm_thread_elements"] if e["type"] == "line"]
        self.assertEqual(len(lines), 4)


@unittest.skipUnless(HAS_GEOM, "solidmind_geometry not built")
class TestThreadProfile(unittest.TestCase):
    """Tests for the thread profile Rust function."""

    def test_m8_coarse(self):
        r = geom.thread_profile("M8", "auto", True, 20)
        self.assertEqual(r["params"]["designation"], "M8")
        self.assertEqual(r["params"]["thread_type"], "ISO_metric")
        self.assertAlmostEqual(r["params"]["pitch_mm"], 1.25, places=2)
        self.assertAlmostEqual(r["params"]["major_diameter_mm"], 8.0, places=2)

    def test_m8_pitch_diameter(self):
        """M8x1.25 pitch diameter should be ~7.188mm."""
        r = geom.thread_profile("M8", "auto", True, 20)
        self.assertAlmostEqual(r["params"]["pitch_diameter_mm"], 7.188, places=2)

    def test_m8_minor_diameter(self):
        """M8x1.25 minor diameter should be ~6.647mm."""
        r = geom.thread_profile("M8", "auto", True, 20)
        self.assertAlmostEqual(r["params"]["minor_diameter_mm"], 6.647, places=2)

    def test_m8_fine(self):
        r = geom.thread_profile("M8x1", "auto", True, 20)
        self.assertAlmostEqual(r["params"]["pitch_mm"], 1.0, places=2)

    def test_unc_quarter_20(self):
        r = geom.thread_profile("1/4-20", "auto", True, 20)
        self.assertEqual(r["params"]["thread_type"], "UNC")
        self.assertAlmostEqual(r["params"]["pitch_mm"], 25.4 / 20, places=2)

    def test_acme_half_10(self):
        r = geom.thread_profile("1/2-10 ACME", "auto", True, 20)
        self.assertEqual(r["params"]["thread_type"], "ACME")
        self.assertAlmostEqual(r["params"]["thread_angle_deg"], 29.0, places=1)

    def test_internal_vs_external(self):
        ext = geom.thread_profile("M8", "auto", True, 20)
        intr = geom.thread_profile("M8", "auto", False, 20)
        self.assertTrue(ext["params"]["external"])
        self.assertFalse(intr["params"]["external"])

    def test_has_elements(self):
        r = geom.thread_profile("M8", "auto", True, 20)
        self.assertIn("elements", r)
        self.assertGreater(len(r["elements"]), 0)

    def test_has_build_hint(self):
        r = geom.thread_profile("M8", "auto", True, 20)
        self.assertIn("build_hint", r)


@unittest.skipUnless(HAS_GEOM, "solidmind_geometry not built")
class TestHelicalSpring(unittest.TestCase):
    """Tests for the helical spring Rust function."""

    def test_basic_result_keys(self):
        r = geom.helical_spring("compression", 1.5, 12.0, 8.0, 40.0, 79.3, -1.0, "closed_ground", -1.0)
        self.assertIn("wire_elements", r)
        self.assertIn("helix_params", r)
        self.assertIn("analysis", r)
        self.assertIn("build_hint", r)

    def test_spring_rate_formula(self):
        """k = G*d^4 / (8*D^3*Na)."""
        d, D, Na, G = 1.5, 12.0, 8.0, 79.3e3  # G in MPa
        expected_k = G * d**4 / (8 * D**3 * Na)
        r = geom.helical_spring("compression", d, D, Na, 40.0, 79.3, -1.0, "closed_ground", -1.0)
        self.assertAlmostEqual(r["analysis"]["spring_rate"], expected_k, places=1)

    def test_solid_height_closed_ground(self):
        """Solid height = (Na + 2) * d for closed_ground ends."""
        d, Na = 1.5, 8.0
        expected = (Na + 2) * d
        r = geom.helical_spring("compression", d, 12.0, Na, 40.0, 79.3, -1.0, "closed_ground", -1.0)
        self.assertAlmostEqual(r["analysis"]["solid_height"], expected, places=2)

    def test_wahl_factor(self):
        """Kw = (4C-1)/(4C-4) + 0.615/C where C = D/d."""
        d, D = 1.5, 12.0
        C = D / d
        expected_kw = (4 * C - 1) / (4 * C - 4) + 0.615 / C
        r = geom.helical_spring("compression", d, D, 8.0, 40.0, 79.3, -1.0, "closed_ground", -1.0)
        self.assertAlmostEqual(r["analysis"]["wahl_factor"], expected_kw, places=4)

    def test_helix_params(self):
        r = geom.helical_spring("compression", 1.5, 12.0, 8.0, 40.0, 79.3, -1.0, "closed_ground", -1.0)
        hp = r["helix_params"]
        self.assertIn("radius", hp)
        self.assertIn("pitch", hp)
        self.assertIn("height", hp)
        self.assertIn("turns", hp)

    def test_wire_elements_circle(self):
        """Wire cross-section should be a circle."""
        r = geom.helical_spring("compression", 1.5, 12.0, 8.0, 40.0, 79.3, -1.0, "closed_ground", -1.0)
        circles = [e for e in r["wire_elements"] if e["type"] == "circle"]
        self.assertEqual(len(circles), 1)
        self.assertAlmostEqual(circles[0]["r"], 1.5 / 2, places=4)

    def test_buckling_critical(self):
        """Spring with Lf/D > 4 should flag buckling."""
        # Lf=60, D=12 → ratio=5 > 4
        r = geom.helical_spring("compression", 1.5, 12.0, 8.0, 60.0, 79.3, -1.0, "closed_ground", -1.0)
        self.assertTrue(r["analysis"]["buckling_critical"])


@unittest.skipUnless(HAS_GEOM, "solidmind_geometry not built")
class TestCamProfile(unittest.TestCase):
    """Tests for the cam profile Rust function."""

    def _make_rdrd_cam(self, pts=30):
        """Rise-dwell-return-dwell cam for testing."""
        segs = [
            {"start_angle_deg": 0, "end_angle_deg": 90, "rise_mm": 10, "motion_law": "cycloidal"},
            {"start_angle_deg": 90, "end_angle_deg": 180, "rise_mm": 0, "motion_law": "dwell"},
            {"start_angle_deg": 180, "end_angle_deg": 270, "rise_mm": -10, "motion_law": "simple_harmonic"},
            {"start_angle_deg": 270, "end_angle_deg": 360, "rise_mm": 0, "motion_law": "dwell"},
        ]
        return geom.cam_profile(30.0, segs, "roller", 5.0, 0.0, 0.0, pts)

    def test_basic_result_keys(self):
        r = self._make_rdrd_cam()
        self.assertIn("elements", r)
        self.assertIn("max_pressure_angle_deg", r)
        self.assertIn("max_acceleration", r)
        self.assertIn("displacement_curve", r)
        self.assertIn("build_hint", r)

    def test_displacement_starts_and_ends_at_zero(self):
        r = self._make_rdrd_cam()
        dc = r["displacement_curve"]
        self.assertAlmostEqual(dc[0][1], 0.0, places=3)
        self.assertAlmostEqual(dc[-1][1], 0.0, places=3)

    def test_displacement_reaches_peak(self):
        r = self._make_rdrd_cam()
        dc = r["displacement_curve"]
        peak = max(pt[1] for pt in dc)
        self.assertAlmostEqual(peak, 10.0, places=3)

    def test_dwell_segments_constant(self):
        r = self._make_rdrd_cam()
        dc = r["displacement_curve"]
        # Points in 90-180° range should all be ~10mm
        dwell_pts = [pt for pt in dc if 90 <= pt[0] <= 180]
        for pt in dwell_pts:
            self.assertAlmostEqual(pt[1], 10.0, places=3)

    def test_pressure_angle_reasonable(self):
        r = self._make_rdrd_cam()
        self.assertGreater(r["max_pressure_angle_deg"], 0)
        self.assertLess(r["max_pressure_angle_deg"], 45)

    def test_elements_form_closed_profile(self):
        r = self._make_rdrd_cam()
        self.assertGreater(len(r["elements"]), 0)
        # Should have a spline element
        splines = [e for e in r["elements"] if e["type"] == "spline"]
        self.assertGreater(len(splines), 0)

    def test_knife_edge_follower(self):
        segs = [
            {"start_angle_deg": 0, "end_angle_deg": 180, "rise_mm": 5, "motion_law": "polynomial345"},
            {"start_angle_deg": 180, "end_angle_deg": 360, "rise_mm": -5, "motion_law": "polynomial345"},
        ]
        r = geom.cam_profile(20.0, segs, "knife_edge", 0.0, 0.0, 0.0, 30)
        self.assertIn("elements", r)

    def test_flat_face_follower(self):
        segs = [
            {"start_angle_deg": 0, "end_angle_deg": 180, "rise_mm": 5, "motion_law": "cycloidal"},
            {"start_angle_deg": 180, "end_angle_deg": 360, "rise_mm": -5, "motion_law": "cycloidal"},
        ]
        r = geom.cam_profile(25.0, segs, "flat_face", 0.0, 0.0, 0.0, 30)
        self.assertIn("elements", r)


###########################################################################
# MCP tool wrappers for Rust-backed new tools
###########################################################################


class TestRustMCPToolWrappers(unittest.TestCase):
    """Test MCP wrappers for the 5 Rust-backed geometry tools."""

    def setUp(self):
        from server.geometry_store import clear
        clear()

    def tearDown(self):
        from server.geometry_store import clear
        clear()

    @unittest.skipUnless(HAS_GEOM, "solidmind_geometry not built")
    def test_bevel_gear_tool(self):
        from server.geometry_store import retrieve
        from server.tools_geometry import geometry_bevel_gear

        result = geometry_bevel_gear(module=2.0, teeth=20, mate_teeth=30)
        self.assertTrue(result["ok"])
        self.assertIn("geometry_ref", result)
        self.assertIn("params", result)
        self.assertIn("pitch_diameter", result["params"])
        elems = retrieve(result["geometry_ref"])
        self.assertIsNotNone(elems)

    @unittest.skipUnless(HAS_GEOM, "solidmind_geometry not built")
    def test_worm_gear_tool(self):
        from server.geometry_store import retrieve
        from server.tools_geometry import geometry_worm_gear

        result = geometry_worm_gear(axial_module=2.0, worm_starts=1, wheel_teeth=40)
        self.assertTrue(result["ok"])
        self.assertIn("worm_thread_ref", result)
        self.assertIn("wheel_ref", result)
        self.assertIn("params", result)
        self.assertIn("center_distance", result["params"])
        self.assertIn("efficiency", result["params"])
        worm_elems = retrieve(result["worm_thread_ref"])
        wheel_elems = retrieve(result["wheel_ref"])
        self.assertIsNotNone(worm_elems)
        self.assertIsNotNone(wheel_elems)

    @unittest.skipUnless(HAS_GEOM, "solidmind_geometry not built")
    def test_thread_profile_tool(self):
        from server.geometry_store import retrieve
        from server.tools_geometry import geometry_thread_profile

        result = geometry_thread_profile(designation="M8")
        self.assertTrue(result["ok"])
        self.assertIn("geometry_ref", result)
        self.assertIn("params", result)
        self.assertIn("pitch_mm", result["params"])
        self.assertIn("major_diameter_mm", result["params"])
        elems = retrieve(result["geometry_ref"])
        self.assertIsNotNone(elems)

    @unittest.skipUnless(HAS_GEOM, "solidmind_geometry not built")
    def test_helical_spring_tool(self):
        from server.geometry_store import retrieve
        from server.tools_geometry import geometry_helical_spring

        result = geometry_helical_spring(
            spring_type="compression", wire_diameter=1.5,
            coil_diameter=12.0, active_coils=8.0,
            free_length=40.0, material_g_gpa=79.3,
        )
        self.assertTrue(result["ok"])
        self.assertIn("wire_ref", result)
        self.assertIn("helix_params", result)
        self.assertIn("analysis", result)
        self.assertIn("spring_rate", result["analysis"])
        wire_elems = retrieve(result["wire_ref"])
        self.assertIsNotNone(wire_elems)

    @unittest.skipUnless(HAS_GEOM, "solidmind_geometry not built")
    def test_cam_profile_tool(self):
        from server.geometry_store import retrieve
        from server.tools_geometry import geometry_cam_profile

        result = geometry_cam_profile(
            base_radius=30.0,
            segments=[
                {"start_angle_deg": 0, "end_angle_deg": 180, "rise_mm": 10, "motion_law": "cycloidal"},
                {"start_angle_deg": 180, "end_angle_deg": 360, "rise_mm": -10, "motion_law": "cycloidal"},
            ],
        )
        self.assertTrue(result["ok"])
        self.assertIn("geometry_ref", result)
        self.assertIn("max_pressure_angle_deg", result)
        elems = retrieve(result["geometry_ref"])
        self.assertIsNotNone(elems)


if __name__ == "__main__":
    unittest.main()
