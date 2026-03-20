"""Built-in material library for field analysis.

Provides common engineering materials with mechanical and thermal properties.
All values at room temperature (~20 °C) unless noted.
"""
from __future__ import annotations

import logging
from typing import Any

from server.analysis_models import Material

log = logging.getLogger("solidmind.analysis_materials")

# ---------------------------------------------------------------------------
# Material database
# ---------------------------------------------------------------------------

_MATERIALS: dict[str, Material] = {}


def _register(m: Material) -> None:
    _MATERIALS[m.name] = m


# Aluminium
_register(Material(
    name="aluminum_6061_t6",
    youngs_modulus_mpa=68_900,
    poissons_ratio=0.33,
    density_kg_m3=2700,
    yield_strength_mpa=276,
    thermal_conductivity_w_mk=167,
    specific_heat_j_kgk=896,
    thermal_expansion_1_k=23.6e-6,
    electrical_conductivity_s_m=2.51e7,
))

# Steels
_register(Material(
    name="steel_1018",
    youngs_modulus_mpa=205_000,
    poissons_ratio=0.29,
    density_kg_m3=7870,
    yield_strength_mpa=370,
    thermal_conductivity_w_mk=51.9,
    specific_heat_j_kgk=486,
    thermal_expansion_1_k=12.0e-6,
    electrical_conductivity_s_m=5.0e6,
    relative_permeability=100.0,
))

_register(Material(
    name="steel_4140",
    youngs_modulus_mpa=205_000,
    poissons_ratio=0.29,
    density_kg_m3=7850,
    yield_strength_mpa=655,
    thermal_conductivity_w_mk=42.6,
    specific_heat_j_kgk=473,
    thermal_expansion_1_k=12.3e-6,
    electrical_conductivity_s_m=4.0e6,
    relative_permeability=100.0,
))

_register(Material(
    name="stainless_304",
    youngs_modulus_mpa=193_000,
    poissons_ratio=0.29,
    density_kg_m3=8000,
    yield_strength_mpa=215,
    thermal_conductivity_w_mk=16.2,
    specific_heat_j_kgk=500,
    thermal_expansion_1_k=17.3e-6,
    electrical_conductivity_s_m=1.39e6,
    relative_permeability=1.02,
))

# Titanium
_register(Material(
    name="titanium_6al4v",
    youngs_modulus_mpa=113_800,
    poissons_ratio=0.342,
    density_kg_m3=4430,
    yield_strength_mpa=880,
    thermal_conductivity_w_mk=6.7,
    specific_heat_j_kgk=526,
    thermal_expansion_1_k=8.6e-6,
    electrical_conductivity_s_m=5.8e5,
))

# Copper / Brass
_register(Material(
    name="copper_c11000",
    youngs_modulus_mpa=117_000,
    poissons_ratio=0.34,
    density_kg_m3=8940,
    yield_strength_mpa=69,
    thermal_conductivity_w_mk=391,
    specific_heat_j_kgk=385,
    thermal_expansion_1_k=16.5e-6,
    electrical_conductivity_s_m=5.96e7,
))

_register(Material(
    name="brass_c36000",
    youngs_modulus_mpa=97_000,
    poissons_ratio=0.34,
    density_kg_m3=8500,
    yield_strength_mpa=138,
    thermal_conductivity_w_mk=115,
    specific_heat_j_kgk=380,
    thermal_expansion_1_k=20.5e-6,
    electrical_conductivity_s_m=1.59e7,
))

# Plastics
_register(Material(
    name="pla",
    youngs_modulus_mpa=3500,
    poissons_ratio=0.36,
    density_kg_m3=1240,
    yield_strength_mpa=60,
    thermal_conductivity_w_mk=0.13,
    specific_heat_j_kgk=1800,
    thermal_expansion_1_k=68e-6,
))

_register(Material(
    name="abs",
    youngs_modulus_mpa=2300,
    poissons_ratio=0.35,
    density_kg_m3=1040,
    yield_strength_mpa=43,
    thermal_conductivity_w_mk=0.17,
    specific_heat_j_kgk=1400,
    thermal_expansion_1_k=90e-6,
))

_register(Material(
    name="nylon_6",
    youngs_modulus_mpa=2900,
    poissons_ratio=0.39,
    density_kg_m3=1140,
    yield_strength_mpa=70,
    thermal_conductivity_w_mk=0.25,
    specific_heat_j_kgk=1700,
    thermal_expansion_1_k=80e-6,
))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Fuzzy aliases → canonical name
_ALIASES: dict[str, str] = {
    "aluminum": "aluminum_6061_t6",
    "aluminium": "aluminum_6061_t6",
    "al6061": "aluminum_6061_t6",
    "al_6061": "aluminum_6061_t6",
    "6061": "aluminum_6061_t6",
    "steel": "steel_1018",
    "mild_steel": "steel_1018",
    "1018": "steel_1018",
    "4140": "steel_4140",
    "stainless": "stainless_304",
    "ss304": "stainless_304",
    "304": "stainless_304",
    "titanium": "titanium_6al4v",
    "ti64": "titanium_6al4v",
    "ti_6al_4v": "titanium_6al4v",
    "copper": "copper_c11000",
    "brass": "brass_c36000",
    "nylon": "nylon_6",
}


def get_material(name: str) -> Material | None:
    """Look up a material by canonical name or alias. Returns None if not found."""
    key = name.lower().strip().replace("-", "_").replace(" ", "_")
    if key in _MATERIALS:
        return _MATERIALS[key]
    canonical = _ALIASES.get(key)
    if canonical:
        return _MATERIALS.get(canonical)
    return None


def list_materials(category: str | None = None) -> list[dict[str, Any]]:
    """List available materials, optionally filtered by category."""
    _CATEGORIES: dict[str, list[str]] = {
        "metal": [
            "aluminum_6061_t6", "steel_1018", "steel_4140", "stainless_304",
            "titanium_6al4v", "copper_c11000", "brass_c36000",
        ],
        "plastic": ["pla", "abs", "nylon_6"],
    }

    if category:
        names = _CATEGORIES.get(category.lower(), [])
    else:
        names = list(_MATERIALS.keys())

    result: list[dict[str, Any]] = []
    for n in names:
        m = _MATERIALS.get(n)
        if m:
            result.append({
                "name": m.name,
                "youngs_modulus_mpa": m.youngs_modulus_mpa,
                "yield_strength_mpa": m.yield_strength_mpa,
                "density_kg_m3": m.density_kg_m3,
            })
    return result
