"""Planet-carrier builder — chunk 5 of wiring the loops.

Second per-part-class builder after sun_gear. A planet carrier is a
disc with a central shaft bore and N planet-pin bosses arranged on a
pitch circle (PCD). It's the structural mate to ``sun_gear`` in a
planetary gear train, and is the smallest test of the ``carrier``
build_type path through ``worker_entry._build_carrier`` (added in an
earlier session).

Pipeline:

1. From ``sub_spec``, derive ring of ``pin_count`` evenly-spaced pin
   positions on a circle of diameter ``pin_circle_diameter_mm``.
2. Pack a ``carrier`` build_spec (outer disc + bore + pin bosses) and
   dispatch via ``common.dispatch_and_rewrite`` so the orchestrator's
   verify-mode re-measurement is the source of dimensional truth.
3. Rewrite ``interface_actuals`` under the spec's design-friendly keys
   (``bore_dia`` + ``pin_circle_dia``) so verify-mode strategies can
   pick them up.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from orchestrator.worker_builds import common


def _pin_positions_on_pcd(
    pin_count: int,
    pcd_mm: float,
    phase_deg: float = 90.0,
) -> list[list[float]]:
    """Return ``pin_count`` evenly-spaced [x, y] positions on the PCD.

    ``phase_deg`` rotates the pattern; default 90° puts the first pin
    on the +Y axis (top), matching planetary-gear convention.
    """
    if pin_count <= 0 or pcd_mm <= 0:
        return []
    r = pcd_mm / 2.0
    step = 360.0 / pin_count
    return [
        [
            r * math.cos(math.radians(phase_deg + i * step)),
            r * math.sin(math.radians(phase_deg + i * step)),
        ]
        for i in range(pin_count)
    ]


def build_planet_carrier(
    sub_spec: dict[str, Any],
    output_dir: Path,
    interfaces: list[dict[str, Any]] | None = None,
) -> Path:
    """Build a planet-carrier STEP from a worker sub_spec.

    Expected ``sub_spec`` fields (with defaults so a bare
    ``{"name": "planet_carrier"}`` builds):

    ============================  =====  ==========================================
    field                         dflt   meaning
    ============================  =====  ==========================================
    ``name``/``subsystem``         "planet_carrier"
    ``outer_diameter_mm``          40.0  carrier disc outer diameter
    ``thickness_mm``                5.0  plate thickness
    ``bore_diameter_mm``            8.0  central shaft bore (0 to skip)
    ``pin_count``                     3  number of planet pins
    ``pin_circle_diameter_mm``     22.0  PCD on which pins are arrayed
    ``pin_diameter_mm``             4.0  individual pin diameter
    ``pin_height_mm``               6.0  pin boss height above plate
    ``pin_phase_deg``              90.0  rotation of pattern (deg)
    ============================  =====  ==========================================

    ``interfaces`` is forwarded only for the optional interface-id
    override; the auto-measure path is bypassed (``_build_carrier``'s
    auto-measure picks up the wrong cylinder, just like ``_build_gear``).
    """
    part_name = sub_spec.get("name", sub_spec.get("subsystem", "planet_carrier"))
    outer_diameter_mm = float(sub_spec.get("outer_diameter_mm", 40.0))
    thickness_mm = float(sub_spec.get("thickness_mm", 5.0))
    bore_diameter_mm = float(sub_spec.get("bore_diameter_mm", 8.0))
    pin_count = int(sub_spec.get("pin_count", 3))
    pcd_mm = float(sub_spec.get("pin_circle_diameter_mm", 22.0))
    pin_diameter_mm = float(sub_spec.get("pin_diameter_mm", 4.0))
    pin_height_mm = float(sub_spec.get("pin_height_mm", 6.0))
    pin_phase_deg = float(sub_spec.get("pin_phase_deg", 90.0))

    pin_positions = _pin_positions_on_pcd(pin_count, pcd_mm, pin_phase_deg)

    build_spec: dict[str, Any] = dict(sub_spec)
    build_spec["name"] = part_name
    build_spec["build_type"] = "carrier"
    build_spec["outer_radius_mm"] = outer_diameter_mm / 2.0
    build_spec["thickness_mm"] = thickness_mm
    build_spec["bore_diameter_mm"] = bore_diameter_mm
    build_spec["pin_positions"] = pin_positions
    build_spec["pin_diameter_mm"] = pin_diameter_mm
    build_spec["pin_height_mm"] = pin_height_mm
    build_spec["params"] = {
        "outer_diameter_mm": outer_diameter_mm,
        "thickness_mm": thickness_mm,
        "bore_diameter_mm": bore_diameter_mm,
        "pin_count": pin_count,
        "pin_circle_diameter_mm": pcd_mm,
        "pin_diameter_mm": pin_diameter_mm,
        "pin_height_mm": pin_height_mm,
    }
    build_spec.setdefault(
        "envelope_mm",
        [outer_diameter_mm, outer_diameter_mm, thickness_mm + pin_height_mm],
    )

    interface_id = (
        "ifc1" if interfaces is None else (interfaces[0] if interfaces else {}).get("id", "ifc1")
    )

    return common.dispatch_and_rewrite(
        build_spec=build_spec,
        output_dir=output_dir,
        part_name=part_name,
        interface_actuals={
            interface_id: {
                "bore_dia": bore_diameter_mm,
                "pin_circle_dia": pcd_mm,
            },
        },
        notes=(
            f"planet_carrier builder: outer_dia={outer_diameter_mm}, "
            f"bore={bore_diameter_mm}, pins={pin_count}@PCD={pcd_mm}"
        ),
        claimed_mass_kg=0.025,
    )


__all__ = ["build_planet_carrier"]
