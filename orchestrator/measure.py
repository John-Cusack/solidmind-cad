"""Self-verifying measurement of worker outputs.

The orchestrator's ``validator.py`` docstring states its design intent
explicitly:

    "Reimport STEP files, recompute authoritative measurements, and
     validate against frozen contracts. All dimensional truth comes
     from geometry measurement — worker-claimed values are advisory
     only."

This module is what fills that design hole.  It takes a worker's
produced STEP file, imports it independently via the FreeCAD addon's
``import_step`` command, and measures interface dimensions using the
same ``find_holes`` / ``get_dimensions`` tooling the workers use — but
from inside the orchestrator process, after the worker is done and
its live FreeCAD document may be closed.

The output of ``measure_worker_step`` is the
``dict[ifc_id, dict[feature, value_mm]]`` shape that
``validate_worker_result`` already accepts as its ``measurements``
argument.  When the orchestrator calls the validator with these
independently-measured values, ``measurement_source`` is
``"orchestrator"`` and the worker's own metadata.json is treated as
advisory.

``verify_worker_measurements`` wraps ``measure_worker_step`` with a
drift-detection layer that also compares the measurements against the
worker's claimed values (if present).  Drift beyond
``tolerance_rel`` (default 1%) flags the interface for
``FailureCode.MEASUREMENT_DRIFT``.  This is the enforcement of the
"measure, don't trust" principle from
``.claude/rules/orchestrator-protocol.md``.

Requires a running FreeCAD session on the addon socket.  Callers
that cannot assume FreeCAD availability (e.g. the legacy trust-mode
``tests/test_orchestrator_e2e.py``) should set
``verify_measurements=False`` in ``runner.validate_results``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestrator.spec import Interface, MasterSpec, Subsystem

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Verification result
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MeasurementVerification:
    """Outcome of re-measuring a worker's STEP file independently.

    Fields:
        step_load_ok: True if the STEP file was successfully imported.
        bbox_measured_mm: Bounding box dimensions measured from the
            imported STEP (x, y, z in mm).
        volume_measured_mm3: Solid volume measured from the imported
            STEP in mm³.
        interface_actuals_measured: ``{ifc_id: {feature: mm}}`` —
            same shape as the ``measurements`` argument to
            ``validator.validate_worker_result``.  Features that
            couldn't be located on the imported shape are recorded
            as ``None``.
        drift: ``{ifc_id: {feature: drift_ratio}}`` — measured-vs-claimed
            drift ratio (``(measured - claimed) / claimed``), or
            ``None`` if the claim couldn't be compared.
        drift_exceeds_tolerance: List of interface IDs where at least
            one feature's drift exceeded ``tolerance_rel`` (or where
            the feature couldn't be located at all).
        error: Populated if STEP load failed; None on success.
    """

    step_load_ok: bool
    bbox_measured_mm: list[float] = field(default_factory=list)
    volume_measured_mm3: float = 0.0
    interface_actuals_measured: dict[str, dict[str, float | None]] = field(
        default_factory=dict
    )
    drift: dict[str, dict[str, float | None]] = field(default_factory=dict)
    drift_exceeds_tolerance: list[str] = field(default_factory=list)
    error: str | None = None


# ---------------------------------------------------------------------------
# Core measurement
# ---------------------------------------------------------------------------


# Feature-name → measurement-strategy lookup.  Keys are the semantic
# feature labels that appear in ``Interface.validation.check_points``.
# Values are callables ``(cad_client, object_name, doc_name) -> float | None``
# where ``cad_client`` is the module ``server.tools_cad``.
#
# This is deliberately small.  New feature types can be added by
# adding an entry here; contributors don't have to touch the verifier
# core.  See ``_measure_bore_diameter`` for the template.


def _measure_bore_diameter(
    cad: Any,
    object_name: str,
    doc_name: str,
) -> float | None:
    """Measure the diameter of the largest cylindrical through-hole.

    Walks the faces returned by ``cad.cad_find_holes`` and picks the
    hole with the greatest diameter.  Works on both ``PartDesign::Body``
    objects and imported ``Part::Feature`` shapes (the addon's
    ``find_holes`` handler accepts both after the fix in this series).
    Returns None if no cylindrical hole was found.
    """
    try:
        result = cad.cad_find_holes(body=object_name, doc=doc_name)
    except Exception as exc:  # pragma: no cover - exercised in integration
        log.warning("cad_find_holes failed on %s: %s", object_name, exc)
        return None
    holes = result.get("holes") or []
    if not holes:
        return None
    # Pick the largest hole.  For sun_gear-style parts this is the
    # central bore.  Multi-hole parts should use a more specific
    # strategy keyed off the interface's geometry hints.
    largest = max(holes, key=lambda h: h.get("diameter_mm", 0.0))
    return float(largest.get("diameter_mm", 0.0)) or None


def _measure_bbox_diagonal(
    cad: Any,
    object_name: str,
    doc_name: str,
) -> float | None:
    """Measure the maximum bounding-box extent (largest of x/y/z).

    Useful as a coarse drift signal on envelope-bounded features.
    """
    try:
        result = cad.cad_get_body_topology(body=object_name, doc=doc_name)
    except Exception:  # pragma: no cover
        return None
    bbox = result.get("bounding_box") or {}
    dims = [bbox.get("x_len"), bbox.get("y_len"), bbox.get("z_len")]
    dims = [d for d in dims if d is not None]
    return max(dims) if dims else None


_FEATURE_STRATEGIES: dict[str, Any] = {
    "bore_diameter": _measure_bore_diameter,
    "bore_dia": _measure_bore_diameter,
    "central_bore_dia": _measure_bore_diameter,
    "bbox_diagonal": _measure_bbox_diagonal,
}


def _register_strategy(name: str, fn: Any) -> None:
    """Register a new feature-measurement strategy.

    Extension point for downstream part classes — add strategies
    for features not yet covered by the built-in set without
    modifying this module's core.
    """
    _FEATURE_STRATEGIES[name] = fn


def measure_worker_step(
    step_path: Path,
    subsystem: Subsystem,
    interfaces: list[Interface],
) -> dict[str, dict[str, float | None]]:
    """Re-import a worker's STEP file and measure interface dimensions.

    Returns the measurements in the exact shape that
    ``orchestrator.validator.validate_worker_result`` expects as its
    ``measurements`` argument: ``{ifc_id: {feature: mm}}``.

    Features whose strategy is missing from ``_FEATURE_STRATEGIES``
    return None.  Callers should treat None as "orchestrator could
    not measure this feature" rather than "measurement was zero."

    Requires the FreeCAD addon socket to be live on 127.0.0.1:9876
    with a build that includes the ``import_step`` command.
    """
    # Lazy imports so this module can be imported in environments
    # without the FreeCAD bridge available (e.g. mock tests of the
    # validator's drift logic).
    from server import tools_cad as cad

    if not step_path.is_file():
        raise FileNotFoundError(f"Worker STEP file not found: {step_path}")

    import_result = cad.cad_import_step(
        path=str(step_path),
        object_name=f"Verify_{subsystem.id or subsystem.name}",
    )
    doc_name = import_result["doc"]
    obj_name = import_result["object"]

    measurements: dict[str, dict[str, float | None]] = {}
    try:
        for ifc in interfaces:
            if subsystem.name not in (ifc.subsystem_a, ifc.subsystem_b):
                continue
            ifc_measurements: dict[str, float | None] = {}
            for cp in ifc.validation.check_points:
                strategy = _FEATURE_STRATEGIES.get(cp.feature)
                if strategy is None:
                    log.info(
                        "measure_worker_step: no strategy for feature '%s' "
                        "on interface %s — recording None",
                        cp.feature, ifc.id,
                    )
                    ifc_measurements[cp.feature] = None
                    continue
                ifc_measurements[cp.feature] = strategy(cad, obj_name, doc_name)
            measurements[ifc.id] = ifc_measurements
    finally:
        # Leave the import document around if cleanup is hard; the
        # caller's temp dir will be garbage-collected by FreeCAD on
        # shutdown.  Deliberately not calling closeDocument here to
        # avoid surprising side effects on other documents the user
        # has open.
        pass

    return measurements


# ---------------------------------------------------------------------------
# Verification (measure + drift-detection)
# ---------------------------------------------------------------------------


def verify_worker_measurements(
    step_path: Path,
    claimed: dict[str, dict[str, float]] | None,
    subsystem: Subsystem,
    interfaces: list[Interface],
    tolerance_rel: float = 0.01,
) -> MeasurementVerification:
    """Measure a worker's STEP file and compare against its claimed values.

    Args:
        step_path: Path to the worker's STEP file.
        claimed: The ``interface_actuals`` dict from the worker's
            ``metadata.json``, or None if the worker didn't claim
            anything.  Shape: ``{ifc_id: {feature: mm}}``.
        subsystem: The spec subsystem this worker built.
        interfaces: The frozen interfaces on this subsystem.
        tolerance_rel: Maximum allowed relative drift between measured
            and claimed values (default 1%).

    Returns:
        A ``MeasurementVerification`` report with the measurements,
        drift ratios, and the list of interface IDs whose drift
        exceeded the tolerance.

    Never raises.  STEP load failures are reported via
    ``step_load_ok=False`` and ``error``.
    """
    from server import tools_cad as cad

    try:
        measurements = measure_worker_step(step_path, subsystem, interfaces)
    except FileNotFoundError as exc:
        return MeasurementVerification(
            step_load_ok=False,
            error=f"STEP file missing: {exc}",
        )
    except Exception as exc:  # pragma: no cover - integration
        return MeasurementVerification(
            step_load_ok=False,
            error=f"STEP import or measurement failed: {exc}",
        )

    # Also pull bbox / volume from the imported shape for envelope checks.
    # Re-import is idempotent; we accept the overhead for simplicity.
    try:
        bbox_result = cad.cad_import_step(
            path=str(step_path),
            object_name=f"VerifyBbox_{subsystem.id or subsystem.name}",
        )
        bbox = bbox_result.get("bbox_mm") or []
        volume = float(bbox_result.get("volume_mm3") or 0.0)
    except Exception as exc:  # pragma: no cover - integration
        log.warning("bbox re-import failed: %s", exc)
        bbox = []
        volume = 0.0

    # Drift detection — compare measured vs claimed per feature.
    drift: dict[str, dict[str, float | None]] = {}
    exceeded: list[str] = []
    claimed = claimed or {}
    for ifc_id, features in measurements.items():
        ifc_drift: dict[str, float | None] = {}
        ifc_claimed = claimed.get(ifc_id, {}) or {}
        has_drift = False
        for feature, measured_val in features.items():
            claimed_val = ifc_claimed.get(feature)
            if measured_val is None:
                ifc_drift[feature] = None
                # Unmeasurable features count as drift only if the
                # worker claimed a value — if neither the worker nor
                # the orchestrator knows, we can't say it drifted.
                if claimed_val is not None:
                    has_drift = True
                continue
            if claimed_val is None or claimed_val == 0:
                ifc_drift[feature] = None
                continue
            ratio = (measured_val - float(claimed_val)) / float(claimed_val)
            ifc_drift[feature] = ratio
            if abs(ratio) > tolerance_rel:
                has_drift = True
        drift[ifc_id] = ifc_drift
        if has_drift:
            exceeded.append(ifc_id)

    return MeasurementVerification(
        step_load_ok=True,
        bbox_measured_mm=list(bbox),
        volume_measured_mm3=volume,
        interface_actuals_measured=measurements,
        drift=drift,
        drift_exceeds_tolerance=exceeded,
        error=None,
    )


# ---------------------------------------------------------------------------
# Convenience: format a verification report as human-readable text
# ---------------------------------------------------------------------------


def format_verification_report(
    verification: MeasurementVerification,
    subsystem_name: str = "",
) -> str:
    """Pretty-print a MeasurementVerification for logs and failure reports."""
    lines = [f"MeasurementVerification for {subsystem_name or '<unknown>'}:"]
    lines.append(f"  step_load_ok: {verification.step_load_ok}")
    if verification.error:
        lines.append(f"  error: {verification.error}")
        return "\n".join(lines)
    lines.append(
        f"  bbox_mm: {verification.bbox_measured_mm}  "
        f"volume_mm3: {verification.volume_measured_mm3:.1f}"
    )
    for ifc_id, features in verification.interface_actuals_measured.items():
        drift_row = verification.drift.get(ifc_id, {})
        for feature, measured in features.items():
            ratio = drift_row.get(feature)
            ratio_str = f"{ratio * 100:+.2f}%" if ratio is not None else "n/a"
            lines.append(
                f"  {ifc_id}.{feature}: measured={measured} drift={ratio_str}"
            )
    if verification.drift_exceeds_tolerance:
        lines.append(
            f"  DRIFT EXCEEDED TOLERANCE ON: "
            f"{verification.drift_exceeds_tolerance}"
        )
    return "\n".join(lines)
