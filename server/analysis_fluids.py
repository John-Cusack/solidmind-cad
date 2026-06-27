"""Built-in fluid library for CFD analysis (aerodynamic + hydrodynamic).

Provides common fluids with density and viscosity at standard conditions.
"""
from __future__ import annotations

from typing import Any

from server.analysis_models import FlowConditions

# Fluids at 20°C, 1 atm unless noted
_FLUIDS: dict[str, dict[str, float]] = {
    "air": {
        "density_kg_m3": 1.225,
        "viscosity_pa_s": 1.789e-5,
    },
    "air_sea_level": {
        "density_kg_m3": 1.225,
        "viscosity_pa_s": 1.789e-5,
    },
    "air_3000m": {
        "density_kg_m3": 0.909,
        "viscosity_pa_s": 1.694e-5,
    },
    "freshwater": {
        "density_kg_m3": 998.2,
        "viscosity_pa_s": 1.002e-3,
    },
    "seawater": {
        "density_kg_m3": 1025.0,
        "viscosity_pa_s": 1.08e-3,
    },
    "water": {
        "density_kg_m3": 998.2,
        "viscosity_pa_s": 1.002e-3,
    },
    "oil_sae30": {
        "density_kg_m3": 891.0,
        "viscosity_pa_s": 0.29,
    },
    "glycerin": {
        "density_kg_m3": 1261.0,
        "viscosity_pa_s": 1.412,
    },
}

_ALIASES: dict[str, str] = {
    "sea_water": "seawater",
    "ocean": "seawater",
    "fresh_water": "freshwater",
    "oil": "oil_sae30",
}


def get_fluid(name: str) -> dict[str, float] | None:
    """Look up fluid properties by name. Returns None if not found."""
    key = name.lower().strip().replace("-", "_").replace(" ", "_")
    if key in _FLUIDS:
        return dict(_FLUIDS[key])
    canonical = _ALIASES.get(key)
    if canonical:
        return dict(_FLUIDS[canonical])
    return None


def list_fluids() -> list[dict[str, Any]]:
    """List all available fluids with their properties."""
    return [
        {"name": name, **props}
        for name, props in _FLUIDS.items()
    ]


def make_flow_conditions(
    fluid: str,
    velocity_m_s: float,
    angle_of_attack_deg: float = 0.0,
    sideslip_deg: float = 0.0,
) -> FlowConditions | None:
    """Create FlowConditions from a fluid name and velocity."""
    props = get_fluid(fluid)
    if props is None:
        return None
    return FlowConditions(
        velocity_m_s=velocity_m_s,
        density_kg_m3=props["density_kg_m3"],
        viscosity_pa_s=props["viscosity_pa_s"],
        angle_of_attack_deg=angle_of_attack_deg,
        sideslip_deg=sideslip_deg,
    )
