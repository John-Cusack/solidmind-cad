"""FreeCAD geometry builder for the foam-dart spring launcher.

Drives the FreeCAD addon over the socket (same transport as the other
``worker_builds`` modules) to build real bodies and export real STEP per part.
Requires a live addon on :9876 — ``run.py`` gates on ``common.freecad_ready()``
before calling :func:`build_all`, so this module assumes the socket is up.

Geometry here is deliberately print-friendly: circles padded into tubes/rods,
through-bores pocketed, a flat stand base — no supports, flat-on-bed faces.
Dimensions come from the committed design brief.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from orchestrator.worker_builds import common


def _send(cmd: str, **args: Any) -> dict[str, Any]:
    from server.freecad_client import FreeCADClient

    client = FreeCADClient(host=common.fc_host(), port=common.fc_port())
    client.connect(timeout=5.0)
    try:
        return client.send_command(cmd, timeout=120.0, **args)
    finally:
        client.disconnect()


def _circle(radius_mm: float, cx: float = 0.0, cy: float = 0.0) -> dict[str, Any]:
    # The addon's sketch_populate reads circle radius from "r" (commands.py).
    return {"type": "circle", "cx": cx, "cy": cy, "r": radius_mm}


def _rect(w: float, h: float, x: float = 0.0, y: float = 0.0) -> dict[str, Any]:
    return {"type": "rect", "x": x, "y": y, "w": w, "h": h}


def _tube(
    part: str,
    out_dir: Path,
    *,
    od_mm: float,
    bore_mm: float,
    length_mm: float,
    log: Callable[[str], None],
) -> Path:
    """A padded tube: outer circle extruded, inner bore pocketed through-all."""
    doc = _send("new_document", name=part).get("name", part)
    body = _send("new_body", name=part, doc=doc)["name"]
    sk = _send("new_sketch", body=body, plane="XY", doc=doc)["sketch"]
    _send("sketch_populate", sketch=sk, doc=doc, elements=[_circle(od_mm / 2.0)])
    _send("close_sketch", sketch=sk, doc=doc)
    _send("pad", sketch=sk, length=length_mm, doc=doc)
    if bore_mm > 0.0:
        sk2 = _send("new_sketch", body=body, plane="XY", doc=doc)["sketch"]
        _send("sketch_populate", sketch=sk2, doc=doc, elements=[_circle(bore_mm / 2.0)])
        _send("close_sketch", sketch=sk2, doc=doc)
        _send(
            "pocket", sketch=sk2, pocket_type="ThroughAll", reversed="auto", verify=False, doc=doc
        )
    return _export(part, doc, out_dir, log)


def _rod(
    part: str, out_dir: Path, *, dia_mm: float, length_mm: float, log: Callable[[str], None]
) -> Path:
    doc = _send("new_document", name=part).get("name", part)
    body = _send("new_body", name=part, doc=doc)["name"]
    sk = _send("new_sketch", body=body, plane="XY", doc=doc)["sketch"]
    _send("sketch_populate", sketch=sk, doc=doc, elements=[_circle(dia_mm / 2.0)])
    _send("close_sketch", sketch=sk, doc=doc)
    _send("pad", sketch=sk, length=length_mm, doc=doc)
    return _export(part, doc, out_dir, log)


def _block(
    part: str,
    out_dir: Path,
    *,
    w: float,
    length: float,
    thickness: float,
    log: Callable[[str], None],
) -> Path:
    """A flat rectangular block padded up by ``thickness`` (no supports)."""
    doc = _send("new_document", name=part).get("name", part)
    body = _send("new_body", name=part, doc=doc)["name"]
    sk = _send("new_sketch", body=body, plane="XY", doc=doc)["sketch"]
    _send(
        "sketch_populate", sketch=sk, doc=doc, elements=[_rect(w, length, -w / 2.0, -length / 2.0)]
    )
    _send("close_sketch", sketch=sk, doc=doc)
    _send("pad", sketch=sk, length=thickness, doc=doc)
    return _export(part, doc, out_dir, log)


# --------------------------------------------------------------------------- #
# Enriched latch: a cantilever tooth on a stubby base, with an optional root
# fillet. This is the one part where geometry fidelity matters — the structural
# screen models the tooth as a rectangular cantilever with a fillet stress
# concentration, so the *built* geometry must carry the same tooth + root for a
# faithful screen-vs-FEA comparison (a plain block would have no root SCF).
#
# The profile is an L (base column + thin tooth) drawn in the XY plane and padded
# along +Z by the tooth width. The reflex corner where the tooth meets the column
# is the root; for V2 it is rounded by a tangent arc (radius = fillet_mm). All
# coordinates are computed here in Python so the build needs no fragile
# vertex-index fillet or face-based sketch — only lines, one arc, and coincidence
# constraints. ``latch_profile`` is pure and unit-tested; ``build_latch_variant``
# is the thin addon-driving wrapper.
# --------------------------------------------------------------------------- #
LATCH_BASE_W_MM = 8.0  # column depth in X (the stiff anchor)
LATCH_BASE_H_MM = 12.0  # column height in Y


def latch_profile(
    *,
    root_mm: float,
    fillet_mm: float,
    tooth_len_mm: float,
    base_w_mm: float = LATCH_BASE_W_MM,
    base_h_mm: float = LATCH_BASE_H_MM,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (elements, constraints) for the latch L-profile in the XY plane.

    Vertices, counter-clockwise: A(0,0) B(w,0) C(w,h-root) D(w+len,h-root)
    E(w+len,h) F(0,h). C is the concave tooth root. When ``fillet_mm > 0`` the
    corner at C is replaced by a tangent quarter-arc (the fillet), tangent to the
    column right face (x=w) and the tooth underside (y=h-root).
    """
    w, h, t, r = base_w_mm, base_h_mm, root_mm, fillet_mm
    a = (0.0, 0.0)
    b = (w, 0.0)
    d = (w + tooth_len_mm, h - t)
    e = (w + tooth_len_mm, h)
    f = (0.0, h)

    def line(p1: tuple[float, float], p2: tuple[float, float]) -> dict[str, Any]:
        return {"type": "line", "x1": p1[0], "y1": p1[1], "x2": p2[0], "y2": p2[1]}

    def coincident(i: int, ipos: int, j: int, jpos: int) -> dict[str, Any]:
        return {
            "type": "Coincident",
            "first": i,
            "first_pos": ipos,
            "second": j,
            "second_pos": jpos,
        }

    # A fillet only fits if it is smaller than both adjoining edges.
    use_fillet = r > 1e-6 and r < min(t, w, tooth_len_mm) - 1e-6
    if not use_fillet:
        c = (w, h - t)
        elements = [line(a, b), line(b, c), line(c, d), line(d, e), line(e, f), line(f, a)]
        # End of each line coincides with the start of the next; last closes to first.
        constraints = [coincident(i, 2, (i + 1) % 6, 1) for i in range(6)]
        return elements, constraints

    # Filleted root: shorten B->C to the lower tangent point, arc up to the side
    # tangent point, then continue C->D. Arc center sits r inside the corner.
    c1 = (w, h - t - r)  # tangent point on the column right face (angle 180 deg)
    c2 = (w + r, h - t)  # tangent point on the tooth underside (angle 90 deg)
    arc = {
        "type": "arc",
        "cx": w + r,
        "cy": h - t - r,
        "r": r,
        "start_angle": 90.0,  # arc pos 1 -> c2
        "end_angle": 180.0,  # arc pos 2 -> c1
    }
    # Emission order / indices: 0:A-B 1:B-C1 2:arc 3:C2-D 4:D-E 5:E-F 6:F-A
    elements = [line(a, b), line(b, c1), arc, line(c2, d), line(d, e), line(e, f), line(f, a)]
    constraints = [
        coincident(0, 2, 1, 1),  # A-B end  -> B-C1 start
        coincident(1, 2, 2, 2),  # B-C1 end -> arc end (c1)
        coincident(2, 1, 3, 1),  # arc start (c2) -> C2-D start
        coincident(3, 2, 4, 1),  # C2-D end -> D-E start
        coincident(4, 2, 5, 1),  # D-E end  -> E-F start
        coincident(5, 2, 6, 1),  # E-F end  -> F-A start
        coincident(6, 2, 0, 1),  # F-A end  -> A-B start (close)
    ]
    return elements, constraints


def build_latch_variant(
    out_dir: Path,
    *,
    root_mm: float,
    fillet_mm: float,
    tooth_len_mm: float,
    tooth_width_mm: float,
    label: str,
    log: Callable[[str], None],
) -> dict[str, Any]:
    """Build one latch variant and leave its body live for in-session FEA.

    Returns ``{"part", "doc", "body", "step"}``. The body persists in its own
    document (named ``latch_sear_<label>``) so ``analysis.stress_check`` can run
    against it via ``doc=`` immediately after the build.
    """
    part = f"latch_sear_{label}"
    elements, constraints = latch_profile(
        root_mm=root_mm, fillet_mm=fillet_mm, tooth_len_mm=tooth_len_mm
    )
    doc = _send("new_document", name=part).get("name", part)
    body = _send("new_body", name=part, doc=doc)["name"]
    sk = _send("new_sketch", body=body, plane="XY", doc=doc)["sketch"]
    _send("sketch_populate", sketch=sk, doc=doc, elements=elements, constraints=constraints)
    _send("close_sketch", sketch=sk, doc=doc)
    _send("pad", sketch=sk, length=tooth_width_mm, doc=doc)
    step = _export(part, doc, out_dir, log)
    return {"part": part, "doc": doc, "body": body, "step": step}


def _export(part: str, doc: str, out_dir: Path, log: Callable[[str], None]) -> Path:
    step_path = out_dir / "step" / f"{part}.step"
    stl_path = out_dir / "stl" / f"{part}.stl"
    step_path.parent.mkdir(parents=True, exist_ok=True)
    stl_path.parent.mkdir(parents=True, exist_ok=True)
    _send("export", path=str(step_path), format="step", doc=doc)
    try:
        _send("export", path=str(stl_path), format="stl", doc=doc)
    except Exception:  # pragma: no cover - STL is best-effort
        pass
    log(f"built {part} → {step_path.name}")
    return step_path


def build_all(
    out_dir: Path,
    *,
    specs: dict[str, dict[str, Any]] | None = None,
    log_fn: Callable[[str], None] | None = None,
) -> dict[str, Path]:
    """Build the launcher's printable custom parts → {part: step_path}.

    Dimensions are read from ``specs`` (the committed design brief's per-part
    specs, keyed by part name) so geometry tracks the single source of truth;
    each value falls back to a sensible default if the spec is absent.
    Sequential because the FreeCAD socket is single-document-at-a-time.
    """
    log = log_fn or (lambda _m: None)
    specs = specs or {}

    def g(part: str, key: str, default: float) -> float:
        return float(specs.get(part, {}).get(key, default))

    built: dict[str, Path] = {}
    built["guide_tube"] = _tube(
        "guide_tube",
        out_dir,
        od_mm=g("guide_tube", "bore_dia_mm", 16.0) + 2 * g("guide_tube", "wall_mm", 2.8),
        bore_mm=g("guide_tube", "bore_dia_mm", 16.0),
        length_mm=g("guide_tube", "length_mm", 90.0),
        log=log,
    )
    built["barrel"] = _tube(
        "barrel",
        out_dir,
        od_mm=g("barrel", "bore_dia_mm", 14.5) + 2 * g("barrel", "wall_mm", 2.6),
        bore_mm=g("barrel", "bore_dia_mm", 14.5),
        length_mm=g("barrel", "length_mm", 60.0),
        log=log,
    )
    built["spring_seat"] = _tube(
        "spring_seat",
        out_dir,
        od_mm=g("spring_seat", "seat_dia_mm", 13.0),
        bore_mm=g("spring_seat", "bore_dia_mm", 6.4),
        length_mm=g("spring_seat", "length_mm", 8.0),
        log=log,
    )
    built["plunger_rod"] = _rod(
        "plunger_rod",
        out_dir,
        dia_mm=g("plunger_rod", "dia_mm", 6.0),
        length_mm=g("plunger_rod", "length_mm", 70.0),
        log=log,
    )
    built["plunger_head"] = _rod(
        "plunger_head",
        out_dir,
        dia_mm=g("plunger_head", "dia_mm", 15.2),
        length_mm=g("plunger_head", "thickness_mm", 6.0),
        log=log,
    )
    built["pull_handle"] = _block(
        "pull_handle",
        out_dir,
        w=g("pull_handle", "width_mm", 30.0),
        length=20.0,
        thickness=g("pull_handle", "thickness_mm", 6.0),
        log=log,
    )
    built["latch_sear"] = _block(
        "latch_sear",
        out_dir,
        w=g("latch_sear", "tooth_width_mm", 6.0) + 6.0,
        length=15.0,
        thickness=6.0,
        log=log,
    )
    built["notch_plate"] = _block(
        "notch_plate", out_dir, w=12.0, length=40.0, thickness=3.0, log=log
    )
    built["test_stand"] = _block(
        "test_stand",
        out_dir,
        w=g("test_stand", "base_w_mm", 80.0),
        length=g("test_stand", "base_l_mm", 140.0),
        thickness=6.0,
        log=log,
    )
    return built
