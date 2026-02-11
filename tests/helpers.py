from __future__ import annotations


def make_base_spec_draft(*, maturity_level: str = "L1") -> dict:
    return {
        "meta": {
            "spec_version": "1.0.0",
            "created_at": "2026-02-10T00:00:00Z",
            "process": "cnc",
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
        "manufacturing": {
            "process_notes": "",
            "material": {"family": "", "grade": ""},
            "tolerances": {"general": "", "critical": []},
            "surface_finish": {"ra_um": None, "coating": "none"},
            "cosmetics": {"visible_surfaces": ""},
        },
        "inspection": {"ctq": [], "method": "", "requirements": []},
        "deliverables": {"cad_formats": [], "drawing_required": False},
        "open_questions": [],
        "assumptions": [],
        "_interview": {"answered": {}, "skipped": {}, "_counter": 0},
        "_audit": [],
    }

