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

import math
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


def _corner_arc(
    prev: tuple[float, float],
    vtx: tuple[float, float],
    nxt: tuple[float, float],
    r: float,
) -> tuple[dict[str, Any], tuple[float, float], tuple[float, float], tuple[int, int]]:
    """Tangent fillet arc for an axis-aligned 90° corner of a CCW polygon.

    Returns ``(arc, entry_point, exit_point, (head_pos, tail_pos))`` where the
    polygon, traversed CCW, arrives at ``entry_point`` and leaves at
    ``exit_point``. ``head_pos``/``tail_pos`` are the sketch endpoint indices
    (1 = start_angle point, 2 = end_angle point) that those two points map to —
    they differ for convex vs reflex corners because the minor arc sweeps the
    opposite way. The arc dict is the minor (90°) wedge with ``start_angle <
    end_angle`` so the addon draws it CCW.
    """
    ux, uy = vtx[0] - prev[0], vtx[1] - prev[1]
    ul = math.hypot(ux, uy)
    ux, uy = ux / ul, uy / ul  # incoming edge direction
    wx, wy = nxt[0] - vtx[0], nxt[1] - vtx[1]
    wl = math.hypot(wx, wy)
    wx, wy = wx / wl, wy / wl  # outgoing edge direction

    cross = ux * wy - uy * wx
    # Interior normal of the incoming edge: left for a convex CCW corner, right
    # for a reflex one (the tooth root). The arc centre sits r along it from the
    # incoming tangent point.
    nrmx, nrmy = (-uy, ux) if cross >= 0 else (uy, -ux)
    t_in = (vtx[0] - r * ux, vtx[1] - r * uy)  # entry tangent point (incoming edge)
    t_out = (vtx[0] + r * wx, vtx[1] + r * wy)  # exit tangent point (outgoing edge)
    cx, cy = t_in[0] + r * nrmx, t_in[1] + r * nrmy
    a_in = math.degrees(math.atan2(t_in[1] - cy, t_in[0] - cx)) % 360.0
    a_out = math.degrees(math.atan2(t_out[1] - cy, t_out[0] - cx)) % 360.0

    if cross >= 0:  # convex: minor arc sweeps CCW entry → exit (like a line: head=1)
        start, end, head_pos, tail_pos = a_in, a_out, 1, 2
    else:  # reflex: minor arc sweeps CCW exit → entry (head=2, as the root always did)
        start, end, head_pos, tail_pos = a_out, a_in, 2, 1
    if end <= start:
        end += 360.0
    arc = {"type": "arc", "cx": cx, "cy": cy, "r": r, "start_angle": start, "end_angle": end}
    return arc, t_in, t_out, (head_pos, tail_pos)


def latch_profile(
    *,
    root_mm: float,
    fillet_mm: float,
    tooth_len_mm: float,
    base_w_mm: float = LATCH_BASE_W_MM,
    base_h_mm: float = LATCH_BASE_H_MM,
    clamp_fillet_mm: float = 0.0,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (elements, constraints) for the latch L-profile in the XY plane.

    Vertices, counter-clockwise: A(0,0) B(w,0) C(w,h-root) D(w+len,h-root)
    E(w+len,h) F(0,h). C is the concave tooth root; ``fillet_mm`` rounds it.

    ``clamp_fillet_mm`` (ROADMAP Move 4 #3) rounds the two foot corners A and B
    where the fixed-clamp face (min-Y) meets the column sides. A perfectly rigid
    fixed face terminating at a sharp geometric corner is a stress singularity
    that re-emerges as the mesh refines past the tooth fillet; relieving the
    geometry there keeps the converged tooth-root peak honest in the limit.
    (A fully de-singularized clamp would also model the support as compliant /
    bonded rather than rigid — solver-side follow-up.) Defaults to 0.0 (sharp),
    which reproduces the original profile element-for-element.

    Each corner is filleted only if its radius fits both adjoining edges;
    otherwise it stays sharp.
    """
    w, h, t = base_w_mm, base_h_mm, root_mm
    # (position, requested radius) per vertex, CCW. A/B are the clamp foot; C is
    # the reflex tooth root; D/E/F stay sharp.
    verts: list[tuple[tuple[float, float], float]] = [
        ((0.0, 0.0), clamp_fillet_mm),  # A foot-left (clamp edge)
        ((w, 0.0), clamp_fillet_mm),  # B foot-right (clamp edge)
        ((w, h - t), fillet_mm),  # C tooth root (reflex)
        ((w + tooth_len_mm, h - t), 0.0),  # D tooth tip lower
        ((w + tooth_len_mm, h), 0.0),  # E tooth tip upper
        ((0.0, h), 0.0),  # F top-left
    ]
    n = len(verts)

    def fits(i: int, r: float) -> bool:
        if r <= 1e-6:
            return False
        (px, py), _ = verts[(i - 1) % n]
        (vx, vy), _ = verts[i]
        (nx, ny), _ = verts[(i + 1) % n]
        in_len = math.hypot(vx - px, vy - py)
        out_len = math.hypot(nx - vx, ny - vy)
        return r < min(in_len, out_len) - 1e-6

    radii = [r if fits(i, r) else 0.0 for i, (_, r) in enumerate(verts)]

    # Per vertex: its entry/exit point and (if filleted) its arc + endpoint meta.
    entry: list[tuple[float, float]] = []
    exit_: list[tuple[float, float]] = []
    arcs: list[dict[str, Any] | None] = []
    arc_meta: list[tuple[int, int] | None] = []
    for i in range(n):
        (vx, vy), _ = verts[i]
        if radii[i] <= 0.0:
            entry.append((vx, vy))
            exit_.append((vx, vy))
            arcs.append(None)
            arc_meta.append(None)
            continue
        prev = verts[(i - 1) % n][0]
        nxt = verts[(i + 1) % n][0]
        arc, t_in, t_out, meta = _corner_arc(prev, (vx, vy), nxt, radii[i])
        entry.append(t_in)
        exit_.append(t_out)
        arcs.append(arc)
        arc_meta.append(meta)

    # Walk the polygon CCW: at each vertex emit its arc (if any), then the edge
    # line from this vertex's exit to the next vertex's entry.
    elements: list[dict[str, Any]] = []
    meta_seq: list[tuple[int, int]] = []  # (head_pos, tail_pos) per emitted element
    for i in range(n):
        if arcs[i] is not None:
            elements.append(arcs[i])
            meta_seq.append(arc_meta[i])  # type: ignore[arg-type]
        p1 = exit_[i]
        p2 = entry[(i + 1) % n]
        elements.append({"type": "line", "x1": p1[0], "y1": p1[1], "x2": p2[0], "y2": p2[1]})
        meta_seq.append((1, 2))  # a line's head is pos 1, tail is pos 2

    # Each element's tail coincides with the next element's head; close the loop.
    m = len(elements)
    constraints = [
        {
            "type": "Coincident",
            "first": k,
            "first_pos": meta_seq[k][1],
            "second": (k + 1) % m,
            "second_pos": meta_seq[(k + 1) % m][0],
        }
        for k in range(m)
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
    clamp_fillet_mm: float = 0.0,
) -> dict[str, Any]:
    """Build one latch variant and leave its body live for in-session FEA.

    Returns ``{"part", "doc", "body", "step"}``. The body persists in its own
    document (named ``latch_sear_<label>``) so ``analysis.stress_check`` can run
    against it via ``doc=`` immediately after the build. ``clamp_fillet_mm``
    relieves the fixed-clamp foot corners (ROADMAP Move 4 #3).
    """
    part = f"latch_sear_{label}"
    elements, constraints = latch_profile(
        root_mm=root_mm,
        fillet_mm=fillet_mm,
        tooth_len_mm=tooth_len_mm,
        clamp_fillet_mm=clamp_fillet_mm,
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
