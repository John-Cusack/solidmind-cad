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

from pathlib import Path
from typing import Any, Callable

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
    return {"type": "circle", "cx": cx, "cy": cy, "radius": radius_mm}


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


def _stand(part: str, out_dir: Path, *, base_w: float, base_l: float,
           thickness: float, log: Callable[[str], None]) -> Path:
    doc = _send("new_document", name=part).get("name", part)
    body = _send("new_body", name=part, doc=doc)["name"]
    sk = _send("new_sketch", body=body, plane="XY", doc=doc)["sketch"]
    _send("sketch_populate", sketch=sk, doc=doc,
          elements=[_rect(base_w, base_l, -base_w / 2.0, -base_l / 2.0)])
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


def build_all(out_dir: Path, *, log_fn: Callable[[str], None] | None = None) -> dict[str, Path]:
    """Build the launcher's printable parts and return {part: step_path}.

    Dimensions mirror the committed design brief. Sequential because the FreeCAD
    socket is single-document-at-a-time.
    """
    log = log_fn or (lambda _m: None)
    built: dict[str, Path] = {}
    built["guide_tube"] = _tube("guide_tube", out_dir, od_mm=16.0 + 2 * 2.8,
                                bore_mm=16.0, length_mm=90.0, log=log)
    built["barrel"] = _tube("barrel", out_dir, od_mm=14.5 + 2 * 2.6,
                            bore_mm=14.5, length_mm=60.0, log=log)
    built["spring_seat"] = _tube("spring_seat", out_dir, od_mm=13.0,
                                 bore_mm=6.4, length_mm=8.0, log=log)
    built["plunger_rod"] = _rod("plunger_rod", out_dir, dia_mm=6.0, length_mm=70.0, log=log)
    built["plunger_head"] = _rod("plunger_head", out_dir, dia_mm=15.2, length_mm=6.0, log=log)
    built["test_stand"] = _stand("test_stand", out_dir, base_w=80.0, base_l=140.0,
                                 thickness=6.0, log=log)
    return built
