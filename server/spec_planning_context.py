from __future__ import annotations

from typing import Any


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _extract_legacy_envelope(spec: dict[str, Any]) -> dict[str, Any]:
    env = spec.get("envelope", {})
    if not isinstance(env, dict):
        return {}
    out: dict[str, Any] = {}
    for key in ("length", "width", "height"):
        if key in env and env.get(key) is not None:
            out[key] = env.get(key)
    return out


def _extract_finalized_envelope(spec: dict[str, Any], units: str) -> dict[str, Any]:
    part = spec.get("part", {})
    if not isinstance(part, dict):
        return {}
    env = part.get("envelope", {})
    if not isinstance(env, dict):
        return {}

    x = _num(env.get("x"))
    y = _num(env.get("y"))
    z = _num(env.get("z"))
    out: dict[str, Any] = {}
    if x is not None:
        out["length"] = {"value": x, "unit": units}
    if y is not None:
        out["width"] = {"value": y, "unit": units}
    if z is not None:
        out["height"] = {"value": z, "unit": units}
    return out


def _extract_process(spec: dict[str, Any]) -> str:
    process = ""
    meta = spec.get("meta")
    if isinstance(meta, dict):
        process = str(meta.get("process", ""))

    if not process:
        process = str(spec.get("process", ""))

    if process == "print_3d":
        mfg = spec.get("manufacturing", {})
        if isinstance(mfg, dict):
            tech = str(mfg.get("technology", "")).lower()
            if tech == "fdm":
                return "fdm"
        return "fdm"

    if process in ("cnc", "fdm"):
        return process

    # fall back to legacy process field or default
    return "cnc"


def _extract_units(spec: dict[str, Any]) -> str:
    meta = spec.get("meta", {})
    if isinstance(meta, dict):
        units = str(meta.get("units", "mm"))
        return units if units else "mm"
    return "mm"


def normalize_spec_for_planning(spec: dict[str, Any]) -> dict[str, Any]:
    """Normalize finalized spec and legacy test-shape specs to planning shape.

    Output shape is compatible with current geometry planner legacy extractor:
    - envelope.length/width/height as Quantity-like dicts
    - geometry.hole_features / fillets / chamfers if available
    - process as normalized process key (cnc|fdm)
    - derived planning fields for DfM checks
    """
    units = _extract_units(spec)
    envelope = _extract_legacy_envelope(spec)

    if not envelope:
        envelope = _extract_finalized_envelope(spec, units)

    geometry = spec.get("geometry", {})
    if not isinstance(geometry, dict):
        geometry = {}

    # pull optional finalized-spec hints into flat planning fields
    mfg = spec.get("manufacturing", {})
    if not isinstance(mfg, dict):
        mfg = {}

    in_house = mfg.get("in_house_settings", {})
    if not isinstance(in_house, dict):
        in_house = {}

    # derived fields for verification extensions
    derived: dict[str, Any] = {
        "nozzle_diameter_mm": in_house.get("nozzle_diameter_mm"),
        "layer_height_mm": in_house.get("layer_height_mm"),
        "support_policy": in_house.get("support_policy", ""),
        "output_target": mfg.get("output_target", ""),
    }

    if isinstance(spec.get("planning"), dict):
        derived.update(spec["planning"])

    normalized: dict[str, Any] = {
        "process": _extract_process(spec),
        "units": units,
        "envelope": envelope,
        "geometry": geometry,
        "material": spec.get("material", {}),
        "manufacturing": mfg,
        "planning": derived,
        "_source_shape": "finalized" if "part" in spec else "legacy",
    }

    return normalized
