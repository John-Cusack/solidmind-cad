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


def _has_numeric_setting(settings: dict[str, Any]) -> bool:
    for key in ("layer_height_mm", "nozzle_diameter_mm", "wall_count", "infill_percent"):
        if _as_number(settings.get(key)) is not None:
            return True
    return False


def run(spec_draft: dict) -> list[Finding]:
    meta = spec_draft.get("meta") if isinstance(spec_draft, dict) else None
    maturity = meta.get("maturity_level") if isinstance(meta, dict) else None

    findings: list[Finding] = []

    env = _get(spec_draft, "part", "envelope", default={})
    x = _as_number(env.get("x")) if isinstance(env, dict) else None
    y = _as_number(env.get("y")) if isinstance(env, dict) else None
    z = _as_number(env.get("z")) if isinstance(env, dict) else None
    if x is None or y is None or z is None or x <= 0 or y <= 0 or z <= 0:
        findings.append(
            Finding(
                rule_id="print_3d.envelope.required",
                severity=Severity.BLOCK,
                message="Overall envelope (x/y/z) must be provided and > 0.",
                field="/part/envelope",
                question_id="envelope",
                priority=1000,
            )
        )

    qty = _get(spec_draft, "part", "quantity", default=None)
    if isinstance(qty, bool) or not isinstance(qty, int) or qty < 1:
        findings.append(
            Finding(
                rule_id="print_3d.quantity.positive",
                severity=Severity.BLOCK,
                message="Quantity must be an integer >= 1.",
                field="/part/quantity",
                question_id="quantity",
                priority=950,
            )
        )

    if maturity in ("L2", "L3"):
        grade = _get(spec_draft, "manufacturing", "material", "grade", default="")
        if _is_blank_str(grade):
            findings.append(
                Finding(
                    rule_id="print_3d.material.grade.required",
                    severity=Severity.BLOCK,
                    message="Material grade is required at L2/L3 (e.g., PETG, ABS, Nylon).",
                    field="/manufacturing/material/grade",
                    question_id="material_grade",
                    priority=850,
                )
            )

        interfaces = _get(spec_draft, "part", "interfaces", default=[])
        if not isinstance(interfaces, list) or len(interfaces) == 0:
            findings.append(
                Finding(
                    rule_id="print_3d.interfaces.required",
                    severity=Severity.BLOCK,
                    message="Interfaces must be described at L2/L3 (fits, inserts, mating faces, snap features).",
                    field="/part/interfaces",
                    question_id="interfaces",
                    priority=820,
                )
            )

        cad_formats = _get(spec_draft, "deliverables", "cad_formats", default=[])
        if not isinstance(cad_formats, list) or len(cad_formats) == 0:
            findings.append(
                Finding(
                    rule_id="print_3d.deliverables.cad_formats.required",
                    severity=Severity.BLOCK,
                    message="At least one deliverable format is required at L2/L3 (e.g., STL, 3MF, STEP).",
                    field="/deliverables/cad_formats",
                    question_id="cad_formats",
                    priority=800,
                )
            )

        output_target = _get(spec_draft, "manufacturing", "output_target", default="vendor")
        if output_target in ("in_house", "both"):
            settings = _get(spec_draft, "manufacturing", "in_house_settings", default={})
            notes = settings.get("notes", "") if isinstance(settings, dict) else ""
            has_numeric = _has_numeric_setting(settings) if isinstance(settings, dict) else False
            if _is_blank_str(notes) and not has_numeric:
                findings.append(
                    Finding(
                        rule_id="print_3d.in_house_settings.required",
                        severity=Severity.BLOCK,
                        message=(
                            "In-house output target requires print settings notes or numeric settings "
                            "(layer height/nozzle/walls/infill)."
                        ),
                        field="/manufacturing/in_house_settings",
                        question_id="in_house_settings",
                        priority=790,
                    )
                )

    if maturity == "L3":
        tol_general = _get(spec_draft, "manufacturing", "tolerances", "general", default="")
        if _is_blank_str(tol_general):
            findings.append(
                Finding(
                    rule_id="print_3d.tolerances.fit.required",
                    severity=Severity.BLOCK,
                    message="Fit/functional tolerance notes are required at L3.",
                    field="/manufacturing/tolerances/general",
                    question_id="fit_tolerances",
                    priority=780,
                )
            )

        appearance = _get(spec_draft, "manufacturing", "appearance", default={})
        color = appearance.get("color", "") if isinstance(appearance, dict) else ""
        finish = appearance.get("finish", "") if isinstance(appearance, dict) else ""
        if _is_blank_str(color) or _is_blank_str(finish):
            findings.append(
                Finding(
                    rule_id="print_3d.appearance.required",
                    severity=Severity.BLOCK,
                    message="Appearance finish and color are required at L3.",
                    field="/manufacturing/appearance",
                    question_id="appearance",
                    priority=770,
                )
            )

    return findings
