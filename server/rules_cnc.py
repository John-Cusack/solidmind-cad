from __future__ import annotations

from typing import Any

from server.models import Finding, Severity


def _get(d: dict, *keys: str, default: Any = None) -> Any:
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return cur if cur is not None else default


def _is_blank_str(v: Any) -> bool:
    return not isinstance(v, str) or not v.strip()


def _as_number(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


def run(spec_draft: dict) -> list[Finding]:
    meta = spec_draft.get("meta") if isinstance(spec_draft, dict) else None
    maturity = meta.get("maturity_level") if isinstance(meta, dict) else None

    findings: list[Finding] = []

    # Envelope must be known at all maturity levels (hard stop).
    env = _get(spec_draft, "part", "envelope", default={})
    x = _as_number(env.get("x")) if isinstance(env, dict) else None
    y = _as_number(env.get("y")) if isinstance(env, dict) else None
    z = _as_number(env.get("z")) if isinstance(env, dict) else None
    if x is None or y is None or z is None or x <= 0 or y <= 0 or z <= 0:
        findings.append(
            Finding(
                rule_id="cnc.envelope.required",
                severity=Severity.BLOCK,
                message="Overall envelope (x/y/z) must be provided and > 0.",
                field="/part/envelope",
                question_id="envelope",
                priority=1000,
            )
        )

    # Quantity must be positive (hard stop).
    qty = _get(spec_draft, "part", "quantity", default=None)
    if isinstance(qty, bool) or not isinstance(qty, int) or qty < 1:
        findings.append(
            Finding(
                rule_id="cnc.quantity.positive",
                severity=Severity.BLOCK,
                message="Quantity must be an integer >= 1.",
                field="/part/quantity",
                question_id="quantity",
                priority=950,
            )
        )

    # L2/L3: material grade required.
    if maturity in ("L2", "L3"):
        grade = _get(spec_draft, "manufacturing", "material", "grade", default="")
        if _is_blank_str(grade):
            findings.append(
                Finding(
                    rule_id="cnc.material.grade.required",
                    severity=Severity.BLOCK,
                    message="Material grade is required at L2/L3 (e.g., 6061-T6).",
                    field="/manufacturing/material/grade",
                    question_id="material_grade",
                    priority=900,
                )
            )

        interfaces = _get(spec_draft, "part", "interfaces", default=[])
        if not isinstance(interfaces, list) or len(interfaces) == 0:
            findings.append(
                Finding(
                    rule_id="cnc.interfaces.required",
                    severity=Severity.BLOCK,
                    message="Interfaces must be described at L2/L3 (what it mates to, critical patterns, datums).",
                    field="/part/interfaces",
                    question_id="interfaces",
                    priority=850,
                )
            )

        cad_formats = _get(spec_draft, "deliverables", "cad_formats", default=[])
        if not isinstance(cad_formats, list) or len(cad_formats) == 0:
            findings.append(
                Finding(
                    rule_id="cnc.deliverables.cad_formats.required",
                    severity=Severity.BLOCK,
                    message="At least one CAD deliverable format must be specified at L2/L3 (e.g., STEP).",
                    field="/deliverables/cad_formats",
                    question_id="cad_formats",
                    priority=800,
                )
            )

    # L3: tolerance scheme / finish / inspection explicit.
    if maturity == "L3":
        tol_general = _get(spec_draft, "manufacturing", "tolerances", "general", default="")
        if _is_blank_str(tol_general):
            findings.append(
                Finding(
                    rule_id="cnc.tolerances.general.required",
                    severity=Severity.BLOCK,
                    message="General tolerance scheme is required at L3 (e.g., ISO 2768-m).",
                    field="/manufacturing/tolerances/general",
                    question_id="tolerance_general",
                    priority=780,
                )
            )

        ra = _get(spec_draft, "manufacturing", "surface_finish", "ra_um", default=None)
        ra_num = _as_number(ra)
        if ra_num is None or ra_num <= 0:
            findings.append(
                Finding(
                    rule_id="cnc.surface_finish.required",
                    severity=Severity.BLOCK,
                    message="Surface finish (Ra, um) is required at L3.",
                    field="/manufacturing/surface_finish/ra_um",
                    question_id="surface_finish",
                    priority=770,
                )
            )

        insp_method = _get(spec_draft, "inspection", "method", default="")
        if _is_blank_str(insp_method):
            findings.append(
                Finding(
                    rule_id="cnc.inspection.method.required",
                    severity=Severity.BLOCK,
                    message="Inspection method is required at L3 (e.g., calipers, CMM).",
                    field="/inspection/method",
                    question_id="inspection_method",
                    priority=760,
                )
            )

    return findings

