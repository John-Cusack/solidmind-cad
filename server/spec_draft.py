from __future__ import annotations

from copy import deepcopy
from typing import Any

from server.constants import DEFAULT_PROCESS, MATURITY_LEVELS


def deep_copy_spec_draft(spec_draft: dict) -> dict:
    # Tool inputs are untrusted; keep mutations atomic by working on a deep copy.
    return deepcopy(spec_draft)


def ensure_defaults(spec_draft: dict) -> dict:
    """Ensure the minimal 'draft' skeleton exists.

    This is intentionally conservative: it only creates missing containers and
    omits opinionated defaults wherever possible.
    """
    if not isinstance(spec_draft, dict):
        raise TypeError("spec_draft must be a dict")

    meta = spec_draft.setdefault("meta", {})
    if not isinstance(meta, dict):
        raise TypeError("spec_draft.meta must be a dict")

    meta.setdefault("spec_version", "1.0.0")
    meta.setdefault("created_at", "1970-01-01T00:00:00Z")
    meta.setdefault("process", DEFAULT_PROCESS)
    meta.setdefault("maturity_level", "L1")
    meta.setdefault("units", "mm")

    if meta.get("maturity_level") not in MATURITY_LEVELS:
        # Keep whatever the host provided; validation will report it.
        pass

    part = spec_draft.setdefault("part", {})
    if not isinstance(part, dict):
        raise TypeError("spec_draft.part must be a dict")
    part.setdefault("name", "")
    part.setdefault("description", "")
    part.setdefault("quantity", 1)
    part.setdefault("envelope", {"x": 0, "y": 0, "z": 0})
    part.setdefault("interfaces", [])
    part.setdefault("critical_features", [])

    process = meta.get("process")
    if not isinstance(process, str):
        process = DEFAULT_PROCESS

    manufacturing = spec_draft.setdefault("manufacturing", {})
    if not isinstance(manufacturing, dict):
        raise TypeError("spec_draft.manufacturing must be a dict")
    manufacturing.setdefault("process_notes", "")
    manufacturing.setdefault("material", {"family": "", "grade": ""})
    manufacturing.setdefault("tolerances", {"general": "", "critical": []})

    if process == "cnc":
        manufacturing.setdefault("surface_finish", {"ra_um": None, "coating": "none"})
        manufacturing.setdefault("cosmetics", {"visible_surfaces": ""})
    else:
        manufacturing.setdefault("technology", "fdm")
        manufacturing.setdefault("output_target", "vendor")
        manufacturing.setdefault(
            "appearance",
            {
                "color": "",
                "finish": "",
                "support_marks_ok": True,
                "cosmetic_surfaces": [],
            },
        )
        manufacturing.setdefault("post_processing", [])
        manufacturing.setdefault(
            "in_house_settings",
            {
                "notes": "",
                "layer_height_mm": None,
                "nozzle_diameter_mm": None,
                "wall_count": None,
                "infill_percent": None,
                "support_policy": "",
            },
        )

    inspection = spec_draft.setdefault("inspection", {})
    if not isinstance(inspection, dict):
        raise TypeError("spec_draft.inspection must be a dict")
    inspection.setdefault("ctq", [])
    inspection.setdefault("method", "")
    inspection.setdefault("requirements", [])

    deliverables = spec_draft.setdefault("deliverables", {})
    if not isinstance(deliverables, dict):
        raise TypeError("spec_draft.deliverables must be a dict")
    deliverables.setdefault("cad_formats", [])
    deliverables.setdefault("drawing_required", False)

    spec_draft.setdefault("open_questions", [])
    spec_draft.setdefault("assumptions", [])

    interview = spec_draft.setdefault("_interview", {})
    if not isinstance(interview, dict):
        raise TypeError("spec_draft._interview must be a dict")
    interview.setdefault("answered", {})
    interview.setdefault("skipped", {})
    interview.setdefault("_counter", 0)

    if not isinstance(interview.get("answered"), dict):
        interview["answered"] = {}
    if not isinstance(interview.get("skipped"), dict):
        interview["skipped"] = {}
    if not isinstance(interview.get("_counter"), int):
        interview["_counter"] = 0

    audit = spec_draft.setdefault("_audit", [])
    if not isinstance(audit, list):
        raise TypeError("spec_draft._audit must be a list")

    return spec_draft


def strip_internal_fields(spec: dict) -> dict:
    cleaned = deepcopy(spec)
    cleaned.pop("_interview", None)
    cleaned.pop("_audit", None)
    return cleaned
