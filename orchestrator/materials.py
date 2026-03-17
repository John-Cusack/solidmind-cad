"""Material property database for FEA analysis."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Material:
    """Mechanical properties for linear elastic FEA."""

    name: str
    young_modulus_mpa: float      # E
    poisson_ratio: float          # v
    yield_strength_mpa: float     # Sy
    density_kg_m3: float          # rho
    fatigue_limit_mpa: float | None = None  # L3 future


# Common engineering materials
MATERIAL_DB: dict[str, Material] = {
    # Carbon steels
    "AISI_1018": Material("AISI 1018", 205_000, 0.29, 370, 7870, 200),
    "AISI_1045": Material("AISI 1045", 206_000, 0.29, 530, 7850, 280),
    "AISI_4140": Material("AISI 4140", 210_000, 0.29, 655, 7850, 350),
    "AISI_4340": Material("AISI 4340", 205_000, 0.29, 710, 7850, 380),
    # Alloy steels
    "20MnCr5": Material("20MnCr5", 210_000, 0.30, 590, 7850, 310),
    "16MnCr5": Material("16MnCr5", 210_000, 0.30, 540, 7850, 280),
    # Stainless steels
    "AISI_304": Material("AISI 304", 193_000, 0.29, 215, 8000, 120),
    "AISI_316": Material("AISI 316", 193_000, 0.30, 205, 8000, 115),
    "17_4PH": Material("17-4PH", 197_000, 0.28, 1070, 7780, 500),
    # Aluminum
    "Al_6061_T6": Material("Al 6061-T6", 68_900, 0.33, 276, 2700, 97),
    "Al_7075_T6": Material("Al 7075-T6", 71_700, 0.33, 503, 2810, 160),
    "Al_2024_T3": Material("Al 2024-T3", 73_100, 0.33, 345, 2780, 140),
    # Titanium
    "Ti_6Al_4V": Material("Ti-6Al-4V", 113_800, 0.34, 880, 4430, 500),
    # Copper alloys
    "CuZn37": Material("CuZn37 (Brass)", 110_000, 0.34, 200, 8500, 90),
    "C17200": Material("C17200 (BeCu)", 131_000, 0.30, 1035, 8250, 290),
    # Plastics
    "Nylon_6": Material("Nylon 6", 2_800, 0.39, 70, 1140),
    "ABS": Material("ABS", 2_300, 0.35, 40, 1050),
    "POM": Material("POM (Delrin)", 3_100, 0.35, 65, 1410),
}

# Fuzzy aliases: common names -> canonical keys
_ALIASES: dict[str, str] = {
    "steel": "AISI_1045",
    "carbon steel": "AISI_1045",
    "mild steel": "AISI_1018",
    "alloy steel": "AISI_4140",
    "stainless": "AISI_304",
    "stainless steel": "AISI_304",
    "ss304": "AISI_304",
    "ss316": "AISI_316",
    "aluminum": "Al_6061_T6",
    "aluminium": "Al_6061_T6",
    "al6061": "Al_6061_T6",
    "al7075": "Al_7075_T6",
    "titanium": "Ti_6Al_4V",
    "ti64": "Ti_6Al_4V",
    "brass": "CuZn37",
    "nylon": "Nylon_6",
    "abs": "ABS",
    "delrin": "POM",
    "pom": "POM",
    "acetal": "POM",
    "beryllium copper": "C17200",
    "becu": "C17200",
}


def resolve_material(name: str) -> Material | None:
    """Resolve a material name to its properties.

    Tries case-insensitive alias lookup, then exact DB key match.
    Returns None for unknown materials.
    """
    if not name:
        return None
    lower = name.lower().strip()
    # Check aliases first
    canonical = _ALIASES.get(lower)
    if canonical:
        return MATERIAL_DB.get(canonical)
    # Direct key match (case-insensitive)
    for key, mat in MATERIAL_DB.items():
        if key.lower() == lower:
            return mat
    # Try matching the display name
    for mat in MATERIAL_DB.values():
        if mat.name.lower() == lower:
            return mat
    return None
