from __future__ import annotations


def make_base_spec_draft(*, maturity_level: str = "L1", process: str = "print_3d") -> dict:
    if process == "cnc":
        manufacturing = {
            "process_notes": "",
            "material": {"family": "", "grade": ""},
            "tolerances": {"general": "", "critical": []},
            "surface_finish": {"ra_um": None, "coating": "none"},
            "cosmetics": {"visible_surfaces": ""},
        }
    elif process == "print_3d":
        manufacturing = {
            "process_notes": "",
            "technology": "fdm",
            "output_target": "vendor",
            "material": {"family": "", "grade": ""},
            "tolerances": {"general": "", "critical": []},
            "appearance": {"color": "", "finish": "", "support_marks_ok": True, "cosmetic_surfaces": []},
            "post_processing": [],
            "in_house_settings": {
                "notes": "",
                "layer_height_mm": None,
                "nozzle_diameter_mm": None,
                "wall_count": None,
                "infill_percent": None,
                "support_policy": "",
            },
        }
    else:
        raise ValueError(f"Unsupported process for test helper: {process!r}")

    return {
        "meta": {
            "spec_version": "1.0.0",
            "created_at": "2026-02-10T00:00:00Z",
            "process": process,
            "maturity_level": maturity_level,
            "units": "mm",
        },
        "part": {
            "name": "",
            "description": "",
            "quantity": 1,
            "envelope": {"x": 0, "y": 0, "z": 0},
            "interfaces": [],
            "critical_features": [],
        },
        "manufacturing": manufacturing,
        "inspection": {"ctq": [], "method": "", "requirements": []},
        "deliverables": {"cad_formats": [], "drawing_required": False},
        "open_questions": [],
        "assumptions": [],
        "_interview": {"answered": {}, "skipped": {}, "_counter": 0},
        "_audit": [],
    }
