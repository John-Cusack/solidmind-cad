"""Shared element helpers for geometry generators."""
from __future__ import annotations

from typing import Any


def _line(x1: float, y1: float, x2: float, y2: float) -> dict[str, Any]:
    return {"type": "line", "x1": x1, "y1": y1, "x2": x2, "y2": y2}


def _arc(cx: float, cy: float, r: float,
         start_angle: float, end_angle: float) -> dict[str, Any]:
    return {
        "type": "arc", "cx": cx, "cy": cy, "r": r,
        "start_angle": start_angle, "end_angle": end_angle,
    }
