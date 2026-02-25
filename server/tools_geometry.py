from __future__ import annotations

import logging
from typing import Any

from server.geometry_store import store as _store_geometry

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

    return {"ok": True, **result}


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
        sections_out.append({
            "geometry_ref": ref,
            "station_radius_mm": sec["station_radius_mm"],
            "chord_mm": sec["chord_mm"],
            "twist_deg": sec["twist_deg"],
            "plane_offset_mm": sec["plane_offset_mm"],
        })

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
