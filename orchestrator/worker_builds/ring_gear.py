"""Ring-gear builder — internal-toothed annular gear for planetary trains.

Produces the third gear-family piece (after ``sun_gear`` and the planet
gears, both of which are built by ``sun_gear.build_sun_gear``). Routes
through ``worker_entry._build_ring_gear``: outer-blank disc → single
tooth-slot pocket → polar pattern around Z to replicate the slot
``ring_teeth`` times.

The blank and slot sketch elements are computed by
``solidmind_geometry.planetary_layout`` so all three gears in a
planetary train share the same module + pressure-angle + center-
distance constraints. This avoids the orchestrator having to duplicate
gear-math anywhere outside the Rust kernel.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchestrator.worker_builds import common


def _ring_gear_elements(
    module: float,
    sun_teeth: int,
    planet_teeth: int,
    num_planets: int,
    pressure_angle_deg: float = 20.0,
    clearance_coeff: float = 0.25,
    num_involute_pts: int = 20,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """Return ``(blank_elements, slot_elements, ring_teeth)``.

    Delegates to ``solidmind_geometry.planetary_layout`` so the ring's
    geometry is computed from the same primitives as its meshing sun /
    planets — guarantees the assembly condition holds.
    """
    try:
        import solidmind_geometry as geom
    except ImportError as exc:
        raise RuntimeError(
            "solidmind_geometry Rust extension not installed. "
            "Build with: maturin develop --manifest-path geometry/Cargo.toml"
        ) from exc

    layout = geom.planetary_layout(
        module=float(module),
        sun_teeth=int(sun_teeth),
        planet_teeth=int(planet_teeth),
        num_planets=int(num_planets),
        pressure_angle_deg=float(pressure_angle_deg),
        clearance_coeff=float(clearance_coeff),
        profile_shift=0.0,
        backlash=0.0,
        center_x=0.0,
        center_y=0.0,
        num_involute_pts=int(num_involute_pts),
    )
    blank_elements = layout["ring_blank"]["elements"]
    slot_elements = layout["ring_tooth_slot"]["elements"]
    ring_teeth = int(layout["params"]["ring_teeth"])
    return blank_elements, slot_elements, ring_teeth


def build_ring_gear(
    sub_spec: dict[str, Any],
    output_dir: Path,
    interfaces: list[dict[str, Any]] | None = None,
) -> Path:
    """Build a ring-gear STEP from a worker sub_spec.

    ``sub_spec`` fields (defaults size for a 5:1 planetary train —
    sun=12, planet=18, ring=48, num_planets=3):

    ============================  =====  =========================================
    field                         dflt   meaning
    ============================  =====  =========================================
    ``name``/``subsystem``         "ring_gear"
    ``module``                       1.0  gear module (mm/tooth)
    ``sun_teeth``                     12  sun gear tooth count (planetary partner)
    ``planet_teeth``                  18  planet gear tooth count
    ``num_planets``                    3  number of planets in the train
    ``thickness_mm``                10.0  face width (pad length)
    ``pressure_angle_deg``          20.0  involute pressure angle
    ============================  =====  =========================================

    Pre-computed ``blank_elements`` / ``slot_elements`` / ``ring_teeth``
    in the sub_spec override the planetary_layout call (lets a parent
    orchestrator compute the layout once, feed per-gear elements down).
    """
    part_name = sub_spec.get("name", sub_spec.get("subsystem", "ring_gear"))
    module_mm = float(sub_spec.get("module", 1.0))
    thickness_mm = float(sub_spec.get("thickness_mm", 10.0))
    pressure_angle_deg = float(sub_spec.get("pressure_angle_deg", 20.0))

    blank_elements = sub_spec.get("blank_elements")
    slot_elements = sub_spec.get("slot_elements")
    ring_teeth_override = sub_spec.get("ring_teeth")

    if blank_elements is None or slot_elements is None or ring_teeth_override is None:
        blank_elements, slot_elements, ring_teeth = _ring_gear_elements(
            module=module_mm,
            sun_teeth=int(sub_spec.get("sun_teeth", 12)),
            planet_teeth=int(sub_spec.get("planet_teeth", 18)),
            num_planets=int(sub_spec.get("num_planets", 3)),
            pressure_angle_deg=pressure_angle_deg,
        )
    else:
        ring_teeth = int(ring_teeth_override)

    build_spec: dict[str, Any] = dict(sub_spec)
    build_spec["name"] = part_name
    build_spec["build_type"] = "ring_gear"
    build_spec["blank_elements"] = blank_elements
    build_spec["slot_elements"] = slot_elements
    build_spec["ring_teeth"] = ring_teeth
    build_spec["thickness_mm"] = thickness_mm
    build_spec["params"] = {
        "module_mm": module_mm,
        "ring_teeth": ring_teeth,
        "thickness_mm": thickness_mm,
        "pressure_angle_deg": pressure_angle_deg,
    }

    interface_id = (
        "ifc1" if interfaces is None else (interfaces[0] if interfaces else {}).get("id", "ifc1")
    )

    # Internal gears have no physical cylinder at the pitch diameter
    # (the pitch is the rolling circle, virtual). The MEASURABLE feature
    # is the root circle, which on internal teeth sits outward of the
    # pitch by one dedendum:
    #   root_dia = pitch + 2 * dedendum
    #            = ring_teeth*module + 2*(1 + clearance_coeff)*module
    # For the default 20° involute with clearance_coeff=0.25 (matching
    # solidmind_geometry.planetary_layout), that's pitch + 2.5 * module.
    pitch_diameter = ring_teeth * module_mm
    clearance_coeff = float(sub_spec.get("clearance_coeff", 0.25))
    root_diameter = pitch_diameter + 2 * (1 + clearance_coeff) * module_mm

    return common.dispatch_and_rewrite(
        build_spec=build_spec,
        output_dir=output_dir,
        part_name=part_name,
        interface_actuals={
            interface_id: {"bore_dia": root_diameter},
        },
        notes=(
            f"ring_gear builder: module={module_mm}, ring_teeth={ring_teeth}, "
            f"thickness={thickness_mm}, pitch_dia={pitch_diameter}, "
            f"root_dia={root_diameter}"
        ),
        claimed_mass_kg=0.040,
    )


__all__ = ["build_ring_gear"]
