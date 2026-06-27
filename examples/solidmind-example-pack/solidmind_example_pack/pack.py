"""Example extension pack for SolidMind CAD.

Demonstrates both a tool pack and a knowledge pack in one module.
Install with: pip install -e examples/solidmind-example-pack
"""

from __future__ import annotations

import math
from pathlib import Path

# ---------------------------------------------------------------------------
# Tool pack interface
# ---------------------------------------------------------------------------


def bend_allowance(
    *,
    material_thickness: float,
    bend_angle_deg: float,
    bend_radius: float,
    k_factor: float = 0.33,
) -> dict:
    """Calculate sheet metal bend allowance."""
    ba = math.pi / 180 * bend_angle_deg * (bend_radius + k_factor * material_thickness)
    return {
        "ok": True,
        "bend_allowance_mm": round(ba, 3),
        "k_factor": k_factor,
    }


TOOLS: list[dict] = [
    {
        "name": "geometry.bend_allowance",
        "description": "Calculate sheet metal bend allowance and flat pattern length",
        "inputSchema": {
            "type": "object",
            "properties": {
                "material_thickness": {"type": "number", "description": "Sheet thickness (mm)"},
                "bend_angle_deg": {"type": "number", "description": "Bend angle (degrees)"},
                "bend_radius": {"type": "number", "description": "Inside bend radius (mm)"},
                "k_factor": {"type": "number", "default": 0.33, "description": "K-factor (0-1)"},
            },
            "required": ["material_thickness", "bend_angle_deg", "bend_radius"],
        },
    },
]

DISPATCH: dict = {
    "geometry.bend_allowance": bend_allowance,
}

# ---------------------------------------------------------------------------
# Knowledge pack interface
# ---------------------------------------------------------------------------

KNOWLEDGE_DIR: Path = Path(__file__).parent / "knowledge"
DOMAIN: str = "sheetmetal"
VERSION: str = "0.1.0"
