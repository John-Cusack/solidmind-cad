from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

# Ensure repo root is on sys.path so we can reuse jsonutil.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from server.jsonutil import loads as json_loads  # noqa: E402


def _die(msg: str, code: int = 2) -> "None":
    print(msg, file=sys.stderr)
    raise SystemExit(code)


def _load_spec(path: str) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        _die(f"Spec file not found: {path}")
    try:
        data = p.read_bytes()
        obj = json_loads(data)
    except Exception as e:
        _die(f"Failed to read/parse spec JSON: {e}")
    if not isinstance(obj, dict):
        _die("Spec JSON must be an object at the top level")
    return obj


def _require_number(v: Any, name: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        _die(f"Spec field {name} must be a number")
    return float(v)


def _to_mm(value: float, units: str) -> float:
    if units == "mm":
        return value
    if units == "in":
        return value * 25.4
    _die(f"Unsupported units: {units!r} (expected 'mm' or 'in')")


def build_freecad_model(spec: dict[str, Any], out_path: str) -> None:
    try:
        import FreeCAD  # type: ignore
    except Exception as e:
        # Ubuntu packages ship the Python modules here; allow running via plain python3.
        candidate = Path("/usr/lib/freecad-python3/lib")
        if (candidate / "FreeCAD.so").exists() and str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))
            import FreeCAD  # type: ignore  # noqa: F401
        else:
            _die(
                "FreeCAD modules not found.\n"
                "Run this script with FreeCADCmd (headless) or ensure FreeCAD's Python modules are on PYTHONPATH.\n"
                f"Import error: {e}\n"
                "\n"
                "Example (Ubuntu packages):\n"
                "  PYTHONPATH=/usr/lib/freecad-python3/lib python3 scripts/freecad_from_spec.py --spec examples/print_3d/L2.json --out /tmp/part.step\n"
            )

    try:
        import Import  # type: ignore
    except Exception:
        Import = None  # type: ignore

    meta = spec.get("meta", {}) if isinstance(spec, dict) else {}
    part = spec.get("part", {}) if isinstance(spec, dict) else {}
    env = part.get("envelope", {}) if isinstance(part, dict) else {}

    units = meta.get("units", "mm")
    if not isinstance(units, str):
        units = "mm"

    x = _to_mm(_require_number(env.get("x"), "/part/envelope/x"), units)
    y = _to_mm(_require_number(env.get("y"), "/part/envelope/y"), units)
    z = _to_mm(_require_number(env.get("z"), "/part/envelope/z"), units)

    name = part.get("name") if isinstance(part, dict) else None
    if not isinstance(name, str) or not name.strip():
        name = "PartFromSpec"

    doc = FreeCAD.newDocument("SpecModel")
    doc.Label = name

    # Minimal CAD stub: an envelope box. Real feature modeling belongs in a downstream CAD agent.
    box = doc.addObject("Part::Box", "EnvelopeBox")
    box.Length = x
    box.Width = y
    box.Height = z

    # Store a few spec fields as document properties (non-authoritative; for human reference).
    try:
        doc.addProperty("App::PropertyString", "SpecVersion", "Spec", "Spec version").SpecVersion = str(
            meta.get("spec_version", "")
        )
        doc.addProperty("App::PropertyString", "Process", "Spec", "Process").Process = str(meta.get("process", ""))
        doc.addProperty("App::PropertyString", "Maturity", "Spec", "Maturity").Maturity = str(meta.get("maturity_level", ""))
    except Exception:
        pass

    doc.recompute()

    out = Path(out_path)
    suffix = out.suffix.lower()
    if suffix == ".fcstd":
        doc.saveAs(str(out))
        return

    if suffix in (".step", ".stp"):
        if Import is None:
            _die("FreeCAD Import module not available; cannot export STEP in this environment.")
        Import.export([box], str(out))
        return

    _die("Unsupported output extension. Use .FCStd or .step/.stp")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Create a minimal FreeCAD model (envelope box) from a finalized spec JSON.")
    parser.add_argument("--spec", required=True, help="Path to finalized spec JSON (from spec.finalize)")
    parser.add_argument("--out", required=True, help="Output path (.FCStd or .step/.stp)")
    args = parser.parse_args(argv)

    spec = _load_spec(args.spec)
    build_freecad_model(spec, args.out)


if __name__ == "__main__":
    main()
