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

from orchestrator.spec import Interface, Subsystem

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
    interface_actuals_measured: dict[str, dict[str, float | None]] = field(default_factory=dict)
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


def _dedup_cylinders(holes: list[dict[str, Any]]) -> list[float]:
    """Collapse cylindrical face segments with the same diameter.

    ``find_holes`` reports every cylindrical face in the shape, and a
    single logical cylinder (e.g. a sun_gear's central bore or the
    addendum-arc belt around its teeth) can appear as dozens of
    separate segmented faces after STEP import.  This helper groups
    them by rounded diameter and returns the unique diameter list
    sorted ascending.

    A tolerance of 0.001 mm (1 micron) is used for grouping — any two
    faces within that tolerance are considered the same cylinder.
    """
    unique: list[float] = []
    for h in holes:
        d = float(h.get("diameter_mm", 0.0))
        if d <= 0:
            continue
        if not any(abs(d - u) < 0.001 for u in unique):
            unique.append(d)
    unique.sort()
    return unique


def _measure_bore_diameter(
    cad: Any,
    object_name: str,
    doc_name: str,
    expected_mm: float | None = None,
    tolerance_mm: float | None = None,
    step_path: Path | None = None,
) -> float | None:
    """Measure the central-bore cylindrical through-hole diameter.

    Works in two modes:

    1. **With a hint** (``expected_mm`` is provided, typically from the
       interface's ``ValidationCheckPoint.expected_mm``) — picks the
       unique cylinder closest to the expected value.  This is the
       reliable path for any part class where the bore isn't the
       largest or smallest cylinder (e.g. sun_gear, where
       ``find_holes`` also returns the addendum and root-circle arcs).

    2. **No hint** — falls back to the *smallest* unique cylinder on
       the assumption that bores are usually the smallest feature at
       the center.  This is the right fallback for most part classes
       but can fail on parts whose bore is genuinely the largest
       feature (e.g. a thin ring).  Pass ``expected_mm`` whenever the
       interface spec has it — which is essentially always.

    Both modes first deduplicate faces with the same diameter via
    ``_dedup_cylinders`` so the tooth-tip arcs don't count as many
    separate cylinders.  Returns None if no cylindrical face was
    found.
    """
    try:
        result = cad.cad_find_holes(body=object_name, doc=doc_name)
    except Exception as exc:  # pragma: no cover - exercised in integration
        log.warning("cad_find_holes failed on %s: %s", object_name, exc)
        return None
    holes = result.get("holes") or []
    if not holes:
        return None

    unique = _dedup_cylinders(holes)
    if not unique:
        return None

    if expected_mm is not None:
        # Closest-to-expected, no tolerance clipping — the drift
        # detector is what decides whether the pick is "close enough"
        # against the interface's own tolerance_mm. We just report the
        # measurement that matches the LLM's intent.
        best = min(unique, key=lambda d: abs(d - expected_mm))
        log.debug(
            "bore_diameter: expected=%.3f, candidates=%s, picked=%.3f",
            expected_mm,
            unique,
            best,
        )
        return best

    # No hint — assume smallest cylinder is the bore.
    return unique[0]


def _bbox_from_step(cad: Any, step_path: Path | None) -> dict | None:
    """Re-import a STEP into a fresh document and return the bbox dict.

    Workaround for a FreeCAD quirk: ``obj.Shape.BoundBox`` on a Part::Feature
    that was imported via ``import_step`` and then iterated by
    ``find_holes`` returns sentinel ±1e+100 values — the shape's bbox cache
    appears to invalidate after face-iteration mutates ``Surface`` state.
    A fresh import (which calls ``shape.read()`` cleanly and reads bbox
    immediately) gives correct dimensions.
    """
    if step_path is None:
        return None
    try:
        return cad.cad_import_step(
            path=str(step_path),
            object_name=f"BboxProbe_{Path(step_path).stem}",
        )
    except Exception:  # pragma: no cover - integration
        return None


def _measure_bbox_diagonal(
    cad: Any,
    object_name: str,
    doc_name: str,
    expected_mm: float | None = None,
    tolerance_mm: float | None = None,
    step_path: Path | None = None,
) -> float | None:
    """Measure the maximum bounding-box extent (largest of x/y/z).

    Useful as a coarse drift signal on envelope-bounded features.
    ``expected_mm`` / ``tolerance_mm`` are accepted for signature
    uniformity with the other strategies but aren't used here.
    """
    fresh = _bbox_from_step(cad, step_path)
    if fresh is not None:
        bbox_mm = fresh.get("bbox_mm") or []
        if len(bbox_mm) >= 3 and all(abs(d) < 1e50 for d in bbox_mm):
            return max(float(d) for d in bbox_mm[:3])

    try:
        result = cad.cad_get_dimensions(object_name=object_name, doc=doc_name)
    except Exception:  # pragma: no cover
        return None
    bbox = result.get("bounding_box") or {}
    dims = [bbox.get("x_len"), bbox.get("y_len"), bbox.get("z_len")]
    dims = [d for d in dims if d is not None and abs(d) < 1e50]
    return max(dims) if dims else None


def _measure_segment_length(
    cad: Any,
    object_name: str,
    doc_name: str,
    expected_mm: float | None = None,
    tolerance_mm: float | None = None,
    step_path: Path | None = None,
) -> float | None:
    """Return the longest planar bounding-box dimension (max of x_len, y_len).

    Used by hexapod_leg-style parts whose "segment_length" is the laid-out
    extent of the body in the XY plane (z is thickness). Doesn't honor
    ``expected_mm``: a body has exactly one bbox.

    Prefers a fresh STEP re-import for the bbox (see ``_bbox_from_step``
    for why); falls back to ``cad_get_dimensions`` on the live shape.
    """
    fresh = _bbox_from_step(cad, step_path)
    if fresh is not None:
        bbox_mm = fresh.get("bbox_mm") or []
        if len(bbox_mm) >= 2 and all(abs(d) < 1e50 for d in bbox_mm[:2]):
            return max(float(bbox_mm[0]), float(bbox_mm[1]))

    try:
        result = cad.cad_get_dimensions(object_name=object_name, doc=doc_name)
    except Exception:  # pragma: no cover
        return None
    bbox = result.get("bounding_box") or {}
    x_len = bbox.get("x_len")
    y_len = bbox.get("y_len")
    if x_len is None or y_len is None or abs(x_len) > 1e50 or abs(y_len) > 1e50:
        return None
    return max(float(x_len), float(y_len))


def _group_holes_by_diameter(
    holes: list[dict[str, Any]],
    tol_mm: float = 0.001,
) -> list[list[dict[str, Any]]]:
    """Group holes whose diameters agree within ``tol_mm``.

    Returns a list of groups (each a list of hole dicts), preserving
    input order within each group.  Used by the PCD strategy to find
    sets of co-diameter holes that might form a pin/bolt circle.
    """
    groups: list[list[dict[str, Any]]] = []
    for h in holes:
        d = float(h.get("diameter_mm", 0.0))
        if d <= 0:
            continue
        placed = False
        for grp in groups:
            if abs(float(grp[0]["diameter_mm"]) - d) < tol_mm:
                grp.append(h)
                placed = True
                break
        if not placed:
            groups.append([h])
    return groups


def _unique_centers_xy(
    holes: list[dict[str, Any]],
    pos_tol_mm: float = 0.01,
) -> list[tuple[float, float]]:
    """Collapse cylindrical face segments at the same (x, y) into one entry.

    ``find_holes`` reports each cylindrical face — a single bored hole
    can be split into multiple segmented faces after STEP import, all
    sharing the same center.  Round to ``pos_tol_mm`` and dedupe.
    """
    seen: set[tuple[float, float]] = set()
    unique: list[tuple[float, float]] = []
    for h in holes:
        c = h.get("center") or [0.0, 0.0, 0.0]
        key = (round(float(c[0]) / pos_tol_mm), round(float(c[1]) / pos_tol_mm))
        if key in seen:
            continue
        seen.add(key)
        unique.append((float(c[0]), float(c[1])))
    return unique


def _measure_pin_circle_diameter(
    cad: Any,
    object_name: str,
    doc_name: str,
    expected_mm: float | None = None,
    tolerance_mm: float | None = None,
    step_path: Path | None = None,
) -> float | None:
    """Measure pitch-circle diameter (PCD) of a pin / bolt-hole pattern.

    Algorithm:
      1. ``cad_find_holes`` -> all cylindrical faces with center + dia.
      2. Group by diameter (1 micron tol); keep groups with ≥3 unique
         center positions (need 3 points to define a circle).
      3. For each surviving group, compute centroid → mean radius from
         centroid → 2 × mean = candidate PCD.
      4. With ``expected_mm``: pick candidate closest to it.
         Without:   pick the group with the most holes (largest pattern),
                    breaking ties by larger PCD.

    Works for both pin bosses (planet_carrier) and pocket holes (motor
    mount, chassis mounts) — ``find_holes`` returns both since both
    expose cylindrical faces on the body.
    """
    try:
        result = cad.cad_find_holes(body=object_name, doc=doc_name)
    except Exception as exc:  # pragma: no cover - integration
        log.warning("cad_find_holes failed on %s: %s", object_name, exc)
        return None
    holes = result.get("holes") or []
    if len(holes) < 3:
        return None

    candidates: list[tuple[int, float]] = []  # (count, pcd)
    for grp in _group_holes_by_diameter(holes):
        centers = _unique_centers_xy(grp)
        if len(centers) < 3:
            continue
        cx = sum(p[0] for p in centers) / len(centers)
        cy = sum(p[1] for p in centers) / len(centers)
        radii = [((p[0] - cx) ** 2 + (p[1] - cy) ** 2) ** 0.5 for p in centers]
        mean_r = sum(radii) / len(radii)
        # Filter degenerate clusters (all centers at one point).
        if mean_r < 0.5:
            continue
        candidates.append((len(centers), 2.0 * mean_r))

    if not candidates:
        return None

    if expected_mm is not None:
        best = min(candidates, key=lambda c: abs(c[1] - expected_mm))
        log.debug(
            "pin_circle_diameter: expected=%.3f, candidates=%s, picked=%.3f",
            expected_mm,
            candidates,
            best[1],
        )
        return best[1]

    # No hint: largest pattern wins; tie-break on larger PCD.
    candidates.sort(key=lambda c: (-c[0], -c[1]))
    return candidates[0][1]


def _measure_pocket_depth(
    cad: Any,
    object_name: str,
    doc_name: str,
    expected_mm: float | None = None,
    tolerance_mm: float | None = None,
    step_path: Path | None = None,
) -> float | None:
    """Measure depth of a top-face rectangular pocket.

    Algorithm:
      1. ``cad_get_body_topology`` -> all faces with surface_type, normal,
         center, area.
      2. Filter to planar faces with Z-aligned normals (|n.z| ≥ 0.99) and
         non-trivial area.
      3. Top of body = max-z up-facing face. Pocket floors = additional
         up-facing planar faces whose center.z < top.z (below the top
         surface). Their depth = top.z - floor.z.
      4. With ``expected_mm``: closest match. Without: deepest pocket.

    Treats only Z-aligned pockets (the chunk-8 use case). Doesn't handle
    side pockets; new strategies can be registered for those.
    """
    try:
        result = cad.cad_get_body_topology(body=object_name, doc=doc_name)
    except Exception as exc:  # pragma: no cover - integration
        log.warning("cad_get_body_topology failed on %s: %s", object_name, exc)
        return None
    faces = result.get("faces") or []

    up_facing: list[float] = []  # z-centers of up-facing planar faces
    for f in faces:
        if f.get("surface_type") != "Plane":
            continue
        normal = f.get("normal") or [0.0, 0.0, 0.0]
        if abs(float(normal[2])) < 0.99:
            continue
        if float(f.get("area", 0.0)) < 1e-3:
            continue
        center = f.get("center") or [0.0, 0.0, 0.0]
        if float(normal[2]) > 0.99:
            up_facing.append(float(center[2]))

    if len(up_facing) < 2:
        return None

    top_z = max(up_facing)
    depths = [top_z - z for z in up_facing if z < top_z - 0.001]
    if not depths:
        return None

    if expected_mm is not None:
        return min(depths, key=lambda d: abs(d - expected_mm))
    return max(depths)


_FEATURE_STRATEGIES: dict[str, Any] = {
    # Bore strategies (sun_gear, planet_carrier central bore, axles).
    "bore_diameter": _measure_bore_diameter,
    "bore_dia": _measure_bore_diameter,
    "central_bore_dia": _measure_bore_diameter,
    "axle_bore_dia": _measure_bore_diameter,
    "hip_yaw_bore_dia": _measure_bore_diameter,
    "hip_pitch_bore_dia": _measure_bore_diameter,
    "knee_bore_dia": _measure_bore_diameter,
    # Pin/bolt circle strategies.
    "pin_circle_dia": _measure_pin_circle_diameter,
    "pcd_diameter": _measure_pin_circle_diameter,
    "bolt_circle_dia": _measure_pin_circle_diameter,
    "motor_mount_pcd": _measure_pin_circle_diameter,
    "mounting_pcd": _measure_pin_circle_diameter,
    # Pocket-depth strategies.
    "pocket_depth": _measure_pocket_depth,
    "servo_pocket_depth": _measure_pocket_depth,
    # Segment-length / bbox strategies.
    "segment_length": _measure_segment_length,
    "coxa_length": _measure_segment_length,
    "femur_length": _measure_segment_length,
    "tibia_length": _measure_segment_length,
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
                        cp.feature,
                        ifc.id,
                    )
                    ifc_measurements[cp.feature] = None
                    continue
                # Pass the interface's expected / tolerance hints through
                # so the strategy can disambiguate when find_holes (or
                # similar) returns multiple candidate features. E.g. for
                # a sun_gear, bore_dia should pick the 8 mm bore out of a
                # {8, 17.5, 22} candidate set, guided by expected_mm=8.0.
                # ``step_path`` lets strategies that need a clean bbox
                # re-import the STEP fresh — see ``_bbox_from_step``.
                ifc_measurements[cp.feature] = strategy(
                    cad,
                    obj_name,
                    doc_name,
                    expected_mm=cp.expected_mm,
                    tolerance_mm=cp.tolerance_mm,
                    step_path=step_path,
                )
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
    #
    # Caveat: ``Part.Shape().read(step)`` populates the shape but
    # OpenCascade's BoundBox is computed lazily; on freshly-loaded
    # shapes ``shape.BoundBox`` returns ±1e+100 sentinels until the
    # shape is tessellated or face-iterated. The addon's import_step
    # reads BoundBox immediately and so returns those sentinels for
    # most STEPs in the wild. Detect the sentinel and treat bbox as
    # unmeasured rather than poisoning downstream envelope checks
    # with absurd values.
    try:
        bbox_result = cad.cad_import_step(
            path=str(step_path),
            object_name=f"VerifyBbox_{subsystem.id or subsystem.name}",
        )
        raw_bbox = bbox_result.get("bbox_mm") or []
        if raw_bbox and all(abs(float(d)) < 1e50 for d in raw_bbox):
            bbox = [float(d) for d in raw_bbox]
        else:
            log.info(
                "bbox_measured_mm sentinel for %s — leaving bbox empty so the "
                "envelope check falls back to the worker's claimed bbox",
                subsystem.name,
            )
            bbox = []
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
            lines.append(f"  {ifc_id}.{feature}: measured={measured} drift={ratio_str}")
    if verification.drift_exceeds_tolerance:
        lines.append(f"  DRIFT EXCEEDED TOLERANCE ON: {verification.drift_exceeds_tolerance}")
    return "\n".join(lines)
