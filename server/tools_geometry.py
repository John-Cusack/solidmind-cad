from __future__ import annotations

import logging
from typing import Any

from server.belt_drive import belt_drive_layout as _belt_drive_layout
from server.four_bar import four_bar_analysis as _four_bar_analysis
from server.gear_train_solver import gear_train_solver as _gear_train_solver
from server.geometry_store import store as _store_geometry
from server.keyway_data import keyway_profile as _keyway_profile
from server.oring_data import oring_groove as _oring_groove
from server.press_fit import press_fit_bore as _press_fit_bore
from server.ratchet_profile import ratchet_click_profile as _ratchet_click_profile
from server.section_properties import compute_section as _compute_section
from server.turned_profile import turned_profile as _turned_profile

log = logging.getLogger("solidmind.tools_geometry")

try:
    import solidmind_geometry as _geom

    _AVAILABLE = True
except ImportError:
    _AVAILABLE = False
    log.warning("solidmind_geometry not installed — geometry.* tools disabled")


def _require_lib() -> None:
    if not _AVAILABLE:
        raise RuntimeError(
            "solidmind_geometry Rust extension not installed. "
            "Build with: pip install -e . (requires Rust toolchain + maturin)"
        )


def geometry_spur_gear(
    module: float,
    teeth: int,
    pressure_angle_deg: float = 20.0,
    profile_shift: float = 0.0,
    backlash: float = 0.0,
    center_x: float = 0.0,
    center_y: float = 0.0,
    internal: bool = False,
    num_involute_pts: int = 20,
    clearance_coeff: float = 0.25,
) -> dict[str, Any]:
    """Generate a spur gear profile and store elements server-side.

    Returns a ``geometry_ref`` handle instead of the raw elements array.
    Pass the handle to ``cad.sketch(geometry_ref=...)`` to use the geometry.
    """
    _require_lib()
    result = _geom.spur_gear(
        module=module,
        teeth=teeth,
        pressure_angle_deg=pressure_angle_deg,
        clearance_coeff=clearance_coeff,
        profile_shift=profile_shift,
        backlash=backlash,
        center_x=center_x,
        center_y=center_y,
        num_involute_pts=num_involute_pts,
        internal=internal,
    )
    elements = result.pop("elements")
    ref = _store_geometry(elements, metadata={"tool": "spur_gear", "teeth": teeth})
    return {
        "ok": True,
        "geometry_ref": ref,
        "element_count": len(elements),
        **result,
    }


def geometry_tooth_slot(
    module: float,
    teeth: int,
    pressure_angle_deg: float = 20.0,
    profile_shift: float = 0.0,
    backlash: float = 0.0,
    center_x: float = 0.0,
    center_y: float = 0.0,
    num_involute_pts: int = 20,
    clearance_coeff: float = 0.25,
) -> dict[str, Any]:
    """Generate a single tooth slot and store elements server-side.

    Returns a ``geometry_ref`` handle instead of the raw elements array.
    Pass the handle to ``cad.sketch(geometry_ref=...)`` to use the geometry.
    """
    _require_lib()
    result = _geom.tooth_slot(
        module=module,
        teeth=teeth,
        pressure_angle_deg=pressure_angle_deg,
        clearance_coeff=clearance_coeff,
        profile_shift=profile_shift,
        backlash=backlash,
        center_x=center_x,
        center_y=center_y,
        num_involute_pts=num_involute_pts,
    )
    elements = result.pop("elements")
    ref = _store_geometry(elements, metadata={"tool": "tooth_slot", "teeth": teeth})
    return {
        "ok": True,
        "geometry_ref": ref,
        "element_count": len(elements),
        **result,
    }


def geometry_gear_params(
    module: float,
    teeth: int,
    pressure_angle_deg: float = 20.0,
    profile_shift: float = 0.0,
    backlash: float = 0.0,
    internal: bool = False,
    clearance_coeff: float = 0.25,
) -> dict[str, Any]:
    """Compute gear parameters without generating geometry."""
    _require_lib()
    result = _geom.gear_params(
        module=module,
        teeth=teeth,
        pressure_angle_deg=pressure_angle_deg,
        clearance_coeff=clearance_coeff,
        profile_shift=profile_shift,
        backlash=backlash,
        internal=internal,
    )
    return {"ok": True, "params": result}


def geometry_involute_points(
    base_radius: float,
    start_radius: float,
    end_radius: float,
    num_points: int = 20,
) -> dict[str, Any]:
    """Generate points along an involute curve."""
    _require_lib()
    points = _geom.involute_points(
        base_radius=base_radius,
        start_radius=start_radius,
        end_radius=end_radius,
        num_points=num_points,
    )
    return {"ok": True, "points": points}


def geometry_planetary_layout(
    module: float,
    sun_teeth: int,
    planet_teeth: int,
    num_planets: int = 3,
    pressure_angle_deg: float = 20.0,
    profile_shift: float = 0.0,
    backlash: float = 0.0,
    center_x: float = 0.0,
    center_y: float = 0.0,
    num_involute_pts: int = 20,
    clearance_coeff: float = 0.25,
) -> dict[str, Any]:
    """Generate a planetary gear layout and store each gear's elements server-side.

    Returns ``geometry_ref`` handles for sun, planet, and ring instead of raw
    element arrays.  Pass each handle to ``cad.sketch(geometry_ref=...)`` to use.
    """
    _require_lib()
    result = _geom.planetary_layout(
        module=module,
        sun_teeth=sun_teeth,
        planet_teeth=planet_teeth,
        num_planets=num_planets,
        pressure_angle_deg=pressure_angle_deg,
        clearance_coeff=clearance_coeff,
        profile_shift=profile_shift,
        backlash=backlash,
        center_x=center_x,
        center_y=center_y,
        num_involute_pts=num_involute_pts,
    )

    # Store each gear's elements and replace with refs
    def _store_gear(gear_dict: dict[str, Any], label: str) -> dict[str, Any]:
        elements = gear_dict.pop("elements")
        ref = _store_geometry(elements, metadata={"tool": "planetary_layout", "gear": label})
        return {**gear_dict, "geometry_ref": ref, "element_count": len(elements)}

    result["sun"] = _store_gear(result["sun"], "sun")
    result["planet"] = _store_gear(result["planet"], "planet")
    result["ring"] = _store_gear(result["ring"], "ring")
    result["ring_blank"] = _store_gear(result["ring_blank"], "ring_blank")
    result["ring_tooth_slot"] = _store_gear(result["ring_tooth_slot"], "ring_tooth_slot")

    # Compute ring outer radius for convenience
    ring_params = result["params"]["ring"]
    ring_rf = ring_params["root_diameter"] / 2.0
    ring_outer_radius = ring_rf + 1.5 * module

    return {
        "ok": True,
        **result,
        "ring_teeth": result["params"]["ring_teeth"],
        "ring_outer_radius": ring_outer_radius,
        "ring_build_hint": (
            "To build the ring gear correctly (annular band with internal teeth):\n"
            "1. cad.sketch(geometry_ref=ring_blank_ref) → cad.pad(length=thickness)\n"
            "2. cad.sketch(geometry_ref=ring_tooth_slot_ref, plane=top_face) → "
            "cad.pocket(type='ThroughAll')\n"
            "3. cad.polar_pattern(features=['Pocket'], occurrences=ring_teeth)\n"
            "Do NOT pad the full ring profile — it produces a solid disc."
        ),
    }


def geometry_propeller_blade(
    diameter: float,
    pitch: float,
    hub_diameter: float,
    num_blades: int = 2,
    airfoil: str = "NACA4412",
    chord_root: float | None = None,
    chord_tip: float | None = None,
    num_sections: int = 6,
    num_points: int = 40,
) -> dict[str, Any]:
    """Generate a propeller blade definition with airfoil cross-sections.

    Computes NACA 4-digit airfoil profiles at radial stations with chord taper
    and twist derived from pitch geometry.  Returns ``geometry_ref`` handles for
    each section and the hub, a ``blade_table`` for BEMT analysis, and a
    Selig-format ``airfoil_dat`` string for XFOIL.
    """
    _require_lib()
    result = _geom.propeller_blade_py(
        diameter=diameter,
        pitch=pitch,
        hub_diameter=hub_diameter,
        num_blades=num_blades,
        airfoil=airfoil,
        chord_root=chord_root,
        chord_tip=chord_tip,
        num_sections=num_sections,
        num_points=num_points,
    )

    # Store each section's elements and replace with geometry_refs
    sections_out: list[dict[str, Any]] = []
    for i, sec in enumerate(result["sections"]):
        elements = sec.pop("elements")
        ref = _store_geometry(
            elements,
            metadata={"tool": "propeller_blade", "section": i},
        )
        sections_out.append(
            {
                "geometry_ref": ref,
                "station_radius_mm": sec["station_radius_mm"],
                "chord_mm": sec["chord_mm"],
                "twist_deg": sec["twist_deg"],
                "plane_offset_mm": sec["plane_offset_mm"],
            }
        )

    # Store hub elements
    hub = result["hub"]
    hub_elements = hub.pop("elements")
    hub_ref = _store_geometry(
        hub_elements,
        metadata={"tool": "propeller_blade", "part": "hub"},
    )

    return {
        "ok": True,
        "sections": sections_out,
        "hub": {
            "geometry_ref": hub_ref,
            "diameter_mm": hub["diameter_mm"],
            "height_mm": hub["height_mm"],
        },
        "blade_table": result["blade_table"],
        "airfoil_dat": result["airfoil_dat"],
        "params": result["params"],
        "build_hint": (
            "For each section: cad.sketch on an offset XZ datum plane at "
            "plane_offset_mm from center, using geometry_ref. "
            "Then cad.loft across all sections. "
            "Finally cad.polar_pattern with occurrences=num_blades to replicate."
        ),
    }


# ---------------------------------------------------------------------------
# Generalized geometry tools
# ---------------------------------------------------------------------------


def geometry_epicycloidal_tooth_slot(
    module: float,
    teeth: int,
    mating_teeth: int,
    profile_type: str = "epicycloidal",
    pressure_angle_deg: float = 15.0,
    addendum_coeff: float = 0.75,
    dedendum_coeff: float = 0.85,
    backlash: float = 0.0,
    tip_rounding_r: float = 0.0,
    root_fillet_r: float = 0.0,
    center_x: float = 0.0,
    center_y: float = 0.0,
    num_points: int = 20,
) -> dict[str, Any]:
    """Generate an epicycloidal gear tooth slot and store elements server-side.

    Preferred over standard involute for low tooth counts (< ~20) where
    involute profiles undercut. Supports epicycloidal, modified involute,
    and ogival profile types.  Returns a ``geometry_ref`` handle for
    pocket + polar_pattern workflow.
    """
    _require_lib()
    result = _geom.epicycloidal_tooth_slot(
        module=module,
        teeth=teeth,
        mating_teeth=mating_teeth,
        profile_type=profile_type,
        pressure_angle_deg=pressure_angle_deg,
        addendum_coeff=addendum_coeff,
        dedendum_coeff=dedendum_coeff,
        backlash=backlash,
        tip_rounding_r=tip_rounding_r,
        root_fillet_r=root_fillet_r,
        center_x=center_x,
        center_y=center_y,
        num_points=num_points,
    )
    elements = result.pop("elements")
    ref = _store_geometry(elements, metadata={"tool": "epicycloidal_tooth_slot", "teeth": teeth})
    return {
        "ok": True,
        "geometry_ref": ref,
        "element_count": len(elements),
        **result,
    }


def geometry_spiral(
    inner_radius: float,
    outer_radius: float,
    num_turns: float,
    num_points_per_turn: int = 20,
    center_x: float = 0.0,
    center_y: float = 0.0,
    strip_thickness_mm: float | None = None,
    strip_height_mm: float | None = None,
    material_e_gpa: float | None = None,
    material_yield_mpa: float | None = None,
    overcoil_angle_deg: float | None = None,
    overcoil_style: str | None = None,
) -> dict[str, Any]:
    """Generate an Archimedean spiral and store elements server-side.

    Useful for flat springs, scroll compressor profiles, spiral cams, and
    decorative patterns.  Optionally computes spring stiffness and bending
    stress when strip cross-section and material properties are provided.
    Optionally generates a terminal curve (simple arc or Phillips curve).
    """
    _require_lib()
    result = _geom.spiral_py(
        inner_radius=inner_radius,
        outer_radius=outer_radius,
        num_turns=num_turns,
        num_points_per_turn=num_points_per_turn,
        center_x=center_x,
        center_y=center_y,
        strip_thickness_mm=strip_thickness_mm,
        strip_height_mm=strip_height_mm,
        material_e_gpa=material_e_gpa,
        material_yield_mpa=material_yield_mpa,
        overcoil_angle_deg=overcoil_angle_deg,
        overcoil_style=overcoil_style,
    )

    # Store spiral elements
    spiral_dict = result.pop("spiral")
    spiral_elems = spiral_dict.pop("elements")
    spiral_ref = _store_geometry(spiral_elems, metadata={"tool": "spiral", "part": "spiral"})

    out: dict[str, Any] = {
        "ok": True,
        "spiral_ref": spiral_ref,
        "spiral_element_count": len(spiral_elems),
    }

    # Store overcoil if present
    if "overcoil" in result:
        oc_dict = result.pop("overcoil")
        oc_elems = oc_dict.pop("elements")
        oc_ref = _store_geometry(oc_elems, metadata={"tool": "spiral", "part": "overcoil"})
        out["overcoil_ref"] = oc_ref
        out["overcoil_element_count"] = len(oc_elems)

    out.update(result)
    return out


def geometry_spoke_pattern(
    hub_diameter: float,
    rim_inner_diameter: float,
    rim_outer_diameter: float,
    num_spokes: int = 4,
    spoke_style: str = "straight",
    spoke_width_hub: float = 0.8,
    spoke_width_rim: float = 0.6,
    fillet_radius_hub: float = 0.1,
    fillet_radius_rim: float = 0.1,
    center_x: float = 0.0,
    center_y: float = 0.0,
    num_points: int = 10,
) -> dict[str, Any]:
    """Generate a spoke pocket profile and store elements server-side.

    Returns a ``geometry_ref`` for one inter-spoke pocket, suitable for
    pocket + polar_pattern to cut all spoke gaps. Works for wheels,
    pulleys, flywheels, turbine discs — any hub-and-rim component.
    """
    _require_lib()
    result = _geom.spoke_pattern_py(
        hub_diameter=hub_diameter,
        rim_inner_diameter=rim_inner_diameter,
        rim_outer_diameter=rim_outer_diameter,
        num_spokes=num_spokes,
        spoke_style=spoke_style,
        spoke_width_hub=spoke_width_hub,
        spoke_width_rim=spoke_width_rim,
        fillet_radius_hub=fillet_radius_hub,
        fillet_radius_rim=fillet_radius_rim,
        center_x=center_x,
        center_y=center_y,
        num_points=num_points,
    )
    elements = result.pop("elements")
    ref = _store_geometry(elements, metadata={"tool": "spoke_pattern"})
    return {
        "ok": True,
        "geometry_ref": ref,
        "element_count": len(elements),
        **result,
    }


def geometry_ratchet_tooth(
    pitch_diameter: float,
    teeth: int,
    locking_face_angle_deg: float = 5.0,
    drive_face_angle_deg: float = 45.0,
    tooth_height: float | None = None,
    tip_radius: float = 0.0,
    root_radius: float = 0.0,
    center_x: float = 0.0,
    center_y: float = 0.0,
) -> dict[str, Any]:
    """Generate a ratchet tooth slot and store elements server-side.

    Ratchet teeth are asymmetric: steep locking face (< friction angle) and
    gradual driving face. Pure Python — no Rust dependency needed.
    """
    result = _ratchet_click_profile(
        pitch_diameter=pitch_diameter,
        teeth=teeth,
        locking_face_angle_deg=locking_face_angle_deg,
        drive_face_angle_deg=drive_face_angle_deg,
        tooth_height=tooth_height,
        tip_radius=tip_radius,
        root_radius=root_radius,
        center_x=center_x,
        center_y=center_y,
    )
    elements = result.pop("elements")
    ref = _store_geometry(elements, metadata={"tool": "ratchet_tooth", "teeth": teeth})
    return {
        "ok": True,
        "geometry_ref": ref,
        "element_count": len(elements),
        **result,
    }


def geometry_gear_train_solver(
    total_ratio: float,
    num_stages: int = 3,
    module_range: list[float] | None = None,
    max_diameter: float = 0.0,
    min_pinion_teeth: int = 7,
    max_pinion_teeth: int = 15,
    min_wheel_teeth: int = 20,
    max_wheel_teeth: int = 120,
    tolerance: float = 0.001,
) -> dict[str, Any]:
    """Solve for gear train tooth counts achieving a target ratio.

    Finds tooth count combinations for a multi-stage gear train that produce
    the target total_ratio within tolerance. Pure Python — no Rust dependency.
    """
    mr = tuple(module_range) if module_range else (0.5, 2.0)
    return _gear_train_solver(
        total_ratio=total_ratio,
        num_stages=num_stages,
        module_range=mr,
        max_diameter=max_diameter,
        min_pinion_teeth=min_pinion_teeth,
        max_pinion_teeth=max_pinion_teeth,
        min_wheel_teeth=min_wheel_teeth,
        max_wheel_teeth=max_wheel_teeth,
        tolerance=tolerance,
    )


# ---------------------------------------------------------------------------
# Phase 1: Lookup-driven tools (pure Python)
# ---------------------------------------------------------------------------


def geometry_keyway_profile(
    shaft_diameter: float,
    standard: str = "din6885",
    key_length: float | None = None,
) -> dict[str, Any]:
    """Generate a keyway pocket profile for a given shaft diameter.

    Looks up standard key dimensions and returns a geometry_ref for the
    keyway pocket profile suitable for cad.sketch + cad.pocket.
    """
    result = _keyway_profile(
        shaft_diameter=shaft_diameter,
        standard=standard,
        key_length=key_length,
    )
    elements = result.pop("elements")
    ref = _store_geometry(elements, metadata={"tool": "keyway_profile"})
    # Convert spec dataclass to dict for serialization
    spec = result.pop("spec")
    return {
        "ok": True,
        "geometry_ref": ref,
        "element_count": len(elements),
        "key_width": spec.width,
        "key_height": spec.height,
        "shaft_depth": spec.shaft_depth,
        "hub_depth": spec.hub_depth,
        **result,
    }


def geometry_oring_groove(
    application: str = "static_radial",
    groove_type: str = "bore",
    oring_id_mm: float | None = None,
    cross_section_mm: float | None = None,
    dash_number: int | None = None,
) -> dict[str, Any]:
    """Generate an O-ring groove cross-section profile.

    Looks up standard O-ring dimensions and computes groove geometry per
    Parker O-Ring Handbook. Returns a geometry_ref for the groove profile.
    """
    result = _oring_groove(
        dash_number=dash_number,
        oring_id_mm=oring_id_mm,
        cross_section_mm=cross_section_mm,
        application=application,
        groove_type=groove_type,
    )
    elements = result.pop("elements")
    ref = _store_geometry(elements, metadata={"tool": "oring_groove"})
    # Convert oring dataclass to plain values
    oring = result.pop("oring")
    return {
        "ok": True,
        "geometry_ref": ref,
        "element_count": len(elements),
        "oring_id_mm": oring.id_mm,
        "oring_cs_mm": oring.cs_mm,
        **result,
    }


def geometry_section_properties(
    shape: str,
    **params: Any,
) -> dict[str, Any]:
    """Compute structural cross-section properties.

    Pure computation — returns area, centroid, moments of inertia,
    section moduli, and radii of gyration. No geometry_ref.
    """
    result = _compute_section(shape=shape, **params)
    return {"ok": True, **result}


def geometry_belt_drive(
    driver_diameter: float,
    driven_diameter: float,
    center_distance: float,
    belt_type: str = "timing",
    belt_profile: str | None = None,
) -> dict[str, Any]:
    """Compute belt/chain drive layout parameters.

    Returns wrap angles, belt length, speed ratio, and optionally a
    geometry_ref for the pulley groove profile.
    """
    result = _belt_drive_layout(
        driver_diameter=driver_diameter,
        driven_diameter=driven_diameter,
        center_distance=center_distance,
        belt_type=belt_type,
        belt_profile=belt_profile,
    )
    # Store groove profile elements if present
    if "groove_elements" in result:
        elements = result.pop("groove_elements")
        ref = _store_geometry(elements, metadata={"tool": "belt_drive"})
        result["geometry_ref"] = ref
        result["element_count"] = len(elements)
    # Remove non-serializable dataclass from result
    result.pop("profile_spec", None)
    return {"ok": True, **result}


# ---------------------------------------------------------------------------
# Phase 2: Rust gear extensions
# ---------------------------------------------------------------------------


def geometry_bevel_gear(
    module: float,
    teeth: int,
    mate_teeth: int,
    pressure_angle_deg: float = 20.0,
    shaft_angle_deg: float = 90.0,
    face_width: float | None = None,
    center_x: float = 0.0,
    center_y: float = 0.0,
    num_involute_pts: int = 20,
) -> dict[str, Any]:
    """Generate a bevel gear tooth slot profile using Tredgold's approximation.

    Returns a geometry_ref for the back-cone tooth slot, suitable for
    revolution in FreeCAD.
    """
    _require_lib()
    fw = face_width if face_width is not None else 0.0
    result = _geom.bevel_gear(
        module=module,
        teeth=teeth,
        mate_teeth=mate_teeth,
        pressure_angle_deg=pressure_angle_deg,
        shaft_angle_deg=shaft_angle_deg,
        face_width=fw,
        center_x=center_x,
        center_y=center_y,
        num_involute_pts=num_involute_pts,
    )
    elements = result.pop("elements")
    ref = _store_geometry(elements, metadata={"tool": "bevel_gear", "teeth": teeth})
    return {
        "ok": True,
        "geometry_ref": ref,
        "element_count": len(elements),
        **result,
    }


def geometry_worm_gear(
    axial_module: float,
    worm_starts: int,
    wheel_teeth: int,
    pressure_angle_deg: float = 20.0,
    worm_pitch_diameter: float | None = None,
    center_x: float = 0.0,
    center_y: float = 0.0,
    num_points: int = 20,
) -> dict[str, Any]:
    """Generate worm gear pair geometry (worm thread + wheel tooth profile).

    Returns two geometry_refs: worm_thread_ref for helix sweep and
    wheel_ref for standard gear build workflow.
    """
    _require_lib()
    wpd = worm_pitch_diameter if worm_pitch_diameter is not None else 0.0
    result = _geom.worm_gear(
        axial_module=axial_module,
        worm_starts=worm_starts,
        wheel_teeth=wheel_teeth,
        pressure_angle_deg=pressure_angle_deg,
        worm_pitch_diameter=wpd,
        center_x=center_x,
        center_y=center_y,
        num_points=num_points,
    )
    # Store worm thread elements
    worm_elems = result.pop("worm_thread_elements")
    worm_ref = _store_geometry(worm_elems, metadata={"tool": "worm_gear", "part": "worm"})
    # Store wheel profile elements
    wheel_elems = result.pop("wheel_profile_elements")
    wheel_ref = _store_geometry(wheel_elems, metadata={"tool": "worm_gear", "part": "wheel"})
    return {
        "ok": True,
        "worm_thread_ref": worm_ref,
        "worm_thread_element_count": len(worm_elems),
        "wheel_ref": wheel_ref,
        "wheel_element_count": len(wheel_elems),
        **result,
    }


def geometry_thread_profile(
    designation: str,
    thread_type: str | None = None,
    external: bool = True,
    num_points: int = 20,
) -> dict[str, Any]:
    """Generate a thread profile for one period (for helix sweep).

    Parses standard designations (M8, M8x1, 3/8-16, 1/2-10 ACME) and
    returns a geometry_ref for the cross-section.
    """
    _require_lib()
    tt = thread_type if thread_type is not None else ""
    result = _geom.thread_profile(
        designation=designation,
        thread_type=tt,
        external=external,
        num_points=num_points,
    )
    elements = result.pop("elements")
    ref = _store_geometry(elements, metadata={"tool": "thread_profile", "designation": designation})
    return {
        "ok": True,
        "geometry_ref": ref,
        "element_count": len(elements),
        **result,
    }


# ---------------------------------------------------------------------------
# Phase 3: Complex mechanisms
# ---------------------------------------------------------------------------


def geometry_helical_spring(
    spring_type: str = "compression",
    wire_diameter: float = 1.0,
    coil_diameter: float = 10.0,
    active_coils: float = 8.0,
    free_length: float = 40.0,
    material_g_gpa: float = 79.3,
    material_yield_mpa: float | None = None,
    end_type: str = "closed_ground",
    design_load: float | None = None,
) -> dict[str, Any]:
    """Compute helical spring parameters and generate wire cross-section.

    Returns helix_params (for cad.helix), wire_ref geometry_ref (circle
    cross-section for cad.sweep), and spring analysis results.
    """
    _require_lib()
    result = _geom.helical_spring(
        spring_type=spring_type,
        wire_diameter=wire_diameter,
        coil_diameter=coil_diameter,
        active_coils=active_coils,
        free_length=free_length,
        material_g_gpa=material_g_gpa,
        material_yield_mpa=material_yield_mpa if material_yield_mpa is not None else -1.0,
        end_type=end_type,
        design_load=design_load if design_load is not None else -1.0,
    )
    # Store wire cross-section elements
    wire_elems = result.pop("wire_elements")
    wire_ref = _store_geometry(wire_elems, metadata={"tool": "helical_spring", "part": "wire"})
    return {
        "ok": True,
        "wire_ref": wire_ref,
        "wire_element_count": len(wire_elems),
        **result,
    }


def geometry_four_bar(
    ground_length: float,
    input_length: float,
    coupler_length: float,
    output_length: float,
    coupler_point_x: float = 0.0,
    coupler_point_y: float = 0.0,
    input_angle_start: float = 0.0,
    input_angle_end: float = 360.0,
    num_points: int = 100,
) -> dict[str, Any]:
    """Analyze a four-bar linkage and generate coupler curve.

    Returns Grashof classification, coupler curve geometry_ref, transmission
    angle range, dead points, and mechanical advantage.
    """
    result = _four_bar_analysis(
        ground_length=ground_length,
        input_length=input_length,
        coupler_length=coupler_length,
        output_length=output_length,
        coupler_point_x=coupler_point_x,
        coupler_point_y=coupler_point_y,
        input_angle_start=input_angle_start,
        input_angle_end=input_angle_end,
        num_points=num_points,
    )
    elements = result.pop("elements")
    ref = _store_geometry(elements, metadata={"tool": "four_bar"})
    return {
        "ok": True,
        "geometry_ref": ref,
        "element_count": len(elements),
        **result,
    }


def geometry_cam_profile(
    base_radius: float,
    segments: list[dict[str, Any]],
    follower_type: str = "knife_edge",
    follower_radius: float = 0.0,
    rotation: str = "ccw",
    center_x: float = 0.0,
    center_y: float = 0.0,
    num_points_per_segment: int = 50,
) -> dict[str, Any]:
    """Generate a cam profile from motion law segments.

    Returns a geometry_ref for the closed cam outline spline plus analysis
    (max pressure angle, max acceleration, displacement curve).
    """
    _require_lib()
    result = _geom.cam_profile(
        base_radius=base_radius,
        segments=segments,
        follower_type=follower_type,
        follower_radius=follower_radius,
        center_x=center_x,
        center_y=center_y,
        num_points_per_segment=num_points_per_segment,
    )
    elements = result.pop("elements")
    ref = _store_geometry(elements, metadata={"tool": "cam_profile"})
    return {
        "ok": True,
        "geometry_ref": ref,
        "element_count": len(elements),
        **result,
    }


# ---------------------------------------------------------------------------
# Turned profile (body of revolution)
# ---------------------------------------------------------------------------


def geometry_turned_profile(
    segments: list[dict[str, Any]],
    bore_diameter: float = 0.0,
    lead_chamfer: float = 0.0,
    trail_chamfer: float = 0.0,
    center_x: float = 0.0,
    center_y: float = 0.0,
) -> dict[str, Any]:
    """Generate a closed revolution profile from turned segments.

    Each segment defines a cylindrical or tapered section.  Junctions can
    have fillets (stress relief) or chamfers.  Returns a ``geometry_ref``
    for the closed half-profile, suitable for ``cad.revolution``.
    """
    result = _turned_profile(
        segments=segments,
        bore_diameter=bore_diameter,
        lead_chamfer=lead_chamfer,
        trail_chamfer=trail_chamfer,
        center_x=center_x,
        center_y=center_y,
    )
    elements = result.pop("elements")
    ref = _store_geometry(
        elements,
        metadata={"tool": "turned_profile"},
    )
    return {
        "ok": True,
        "geometry_ref": ref,
        "element_count": len(elements),
        **result,
    }


# ---------------------------------------------------------------------------
# Press-fit bore (ISO 286 tolerance lookup + profile)
# ---------------------------------------------------------------------------


def geometry_press_fit_bore(
    nominal_diameter: float,
    fit: str = "press",
    depth: float = 10.0,
    chamfer: float = 0.0,
    counterbore_diameter: float = 0.0,
    counterbore_depth: float = 0.0,
    center_x: float = 0.0,
    center_y: float = 0.0,
) -> dict[str, Any]:
    """Compute bore dimensions for an ISO fit and generate bore profile.

    Looks up ISO 286 tolerances for the specified fit type and nominal
    diameter.  Returns bore dimensions (min/max/target), fit
    characteristics (clearance or interference in μm), and a
    ``geometry_ref`` for the bore cross-section (half-section for
    revolution).  Use preset names (``press``, ``sliding``,
    ``transition``, etc.) or ISO pairs like ``H7p6``.
    """
    result = _press_fit_bore(
        nominal_diameter=nominal_diameter,
        fit=fit,
        depth=depth,
        chamfer=chamfer,
        counterbore_diameter=counterbore_diameter,
        counterbore_depth=counterbore_depth,
        center_x=center_x,
        center_y=center_y,
    )
    elements = result.pop("elements")
    out: dict[str, Any] = {"ok": True}
    if elements:
        ref = _store_geometry(
            elements,
            metadata={"tool": "press_fit_bore"},
        )
        out["geometry_ref"] = ref
        out["element_count"] = len(elements)
    out.update(result)
    return out
