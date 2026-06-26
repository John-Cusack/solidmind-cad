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


def _tube(part: str, out_dir: Path, *, od_mm: float, bore_mm: float,
          length_mm: float, log: Callable[[str], None]) -> Path:
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
        _send("pocket", sketch=sk2, pocket_type="ThroughAll", reversed="auto",
              verify=False, doc=doc)
    return _export(part, doc, out_dir, log)


def _rod(part: str, out_dir: Path, *, dia_mm: float, length_mm: float,
         log: Callable[[str], None]) -> Path:
    doc = _send("new_document", name=part).get("name", part)
    body = _send("new_body", name=part, doc=doc)["name"]
    sk = _send("new_sketch", body=body, plane="XY", doc=doc)["sketch"]
    _send("sketch_populate", sketch=sk, doc=doc, elements=[_circle(dia_mm / 2.0)])
    _send("close_sketch", sketch=sk, doc=doc)
    _send("pad", sketch=sk, length=length_mm, doc=doc)
    return _export(part, doc, out_dir, log)


def _block(part: str, out_dir: Path, *, w: float, length: float,
           thickness: float, log: Callable[[str], None]) -> Path:
    """A flat rectangular block padded up by ``thickness`` (no supports)."""
    doc = _send("new_document", name=part).get("name", part)
    body = _send("new_body", name=part, doc=doc)["name"]
    sk = _send("new_sketch", body=body, plane="XY", doc=doc)["sketch"]
    _send("sketch_populate", sketch=sk, doc=doc,
          elements=[_rect(w, length, -w / 2.0, -length / 2.0)])
    _send("close_sketch", sketch=sk, doc=doc)
    _send("pad", sketch=sk, length=thickness, doc=doc)
    return _export(part, doc, out_dir, log)


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
        "guide_tube", out_dir,
        od_mm=g("guide_tube", "bore_dia_mm", 16.0) + 2 * g("guide_tube", "wall_mm", 2.8),
        bore_mm=g("guide_tube", "bore_dia_mm", 16.0),
        length_mm=g("guide_tube", "length_mm", 90.0), log=log)
    built["barrel"] = _tube(
        "barrel", out_dir,
        od_mm=g("barrel", "bore_dia_mm", 14.5) + 2 * g("barrel", "wall_mm", 2.6),
        bore_mm=g("barrel", "bore_dia_mm", 14.5),
        length_mm=g("barrel", "length_mm", 60.0), log=log)
    built["spring_seat"] = _tube(
        "spring_seat", out_dir,
        od_mm=g("spring_seat", "seat_dia_mm", 13.0),
        bore_mm=g("spring_seat", "bore_dia_mm", 6.4),
        length_mm=g("spring_seat", "length_mm", 8.0), log=log)
    built["plunger_rod"] = _rod(
        "plunger_rod", out_dir,
        dia_mm=g("plunger_rod", "dia_mm", 6.0),
        length_mm=g("plunger_rod", "length_mm", 70.0), log=log)
    built["plunger_head"] = _rod(
        "plunger_head", out_dir,
        dia_mm=g("plunger_head", "dia_mm", 15.2),
        length_mm=g("plunger_head", "thickness_mm", 6.0), log=log)
    built["pull_handle"] = _block(
        "pull_handle", out_dir,
        w=g("pull_handle", "width_mm", 30.0), length=20.0,
        thickness=g("pull_handle", "thickness_mm", 6.0), log=log)
    built["latch_sear"] = _block(
        "latch_sear", out_dir,
        w=g("latch_sear", "tooth_width_mm", 6.0) + 6.0, length=15.0,
        thickness=6.0, log=log)
    built["notch_plate"] = _block(
        "notch_plate", out_dir, w=12.0, length=40.0, thickness=3.0, log=log)
    built["test_stand"] = _block(
        "test_stand", out_dir,
        w=g("test_stand", "base_w_mm", 80.0),
        length=g("test_stand", "base_l_mm", 140.0),
        thickness=6.0, log=log)
    return built
