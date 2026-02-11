from __future__ import annotations

import base64
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

try:
    import cadquery as _cq  # noqa: F401

    CAD_AVAILABLE = True
except ImportError:
    CAD_AVAILABLE = False


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class GenerateResult:
    file_path: Path
    cad_data: str | None  # base64-encoded, only if under max_inline_bytes
    metadata: dict[str, Any]
    warnings: list[str]


@dataclass(frozen=True, slots=True)
class ParsedInterface:
    count: int
    thread: str
    hole_type: str
    diameter_mm: float
    pattern_x: float | None
    pattern_y: float | None
    remainder: str


class CadGenerator(Protocol):
    def generate(
        self,
        spec: dict[str, Any],
        output_format: str,
        output_path: Path,
        options: dict[str, Any],
    ) -> GenerateResult: ...

    def supported_formats(self) -> tuple[str, ...]: ...


# ---------------------------------------------------------------------------
# ISO 273 clearance hole diameters (normal fit)
# ---------------------------------------------------------------------------

CLEARANCE_DIAMETERS: dict[str, float] = {
    "M2.5": 2.9,
    "M3": 3.4,
    "M4": 4.5,
    "M5": 5.5,
    "M6": 6.6,
    "M8": 9.0,
    "M10": 11.0,
    "M12": 13.5,
}

# Heat-set insert hole diameters (recommended bore for soldering-iron insertion)
HEAT_SET_DIAMETERS: dict[str, float] = {
    "M2.5": 3.6,
    "M3": 4.0,
    "M4": 5.3,
    "M5": 6.4,
    "M6": 7.6,
    "M8": 10.0,
}

# Heat-set insert boss outer diameters (wall around insert hole)
HEAT_SET_BOSS_DIAMETERS: dict[str, float] = {
    "M2.5": 6.0,
    "M3": 7.0,
    "M4": 9.0,
    "M5": 11.0,
    "M6": 13.0,
    "M8": 17.0,
}


# ---------------------------------------------------------------------------
# Interface string parser
# ---------------------------------------------------------------------------

_INTERFACE_RE = re.compile(
    r"(?:(\d+)x\s+)?"
    r"(M\d+(?:\.\d+)?)\s+"
    r"(clearance|tapped|counterbore|countersink|through|heat-set|press-fit)\s+"
    r"(?:holes?|inserts?)"
    r"(?:\s+(.*))?",
    re.IGNORECASE,
)

_PATTERN_HINT_RE = re.compile(
    r"on\s+(\d+(?:\.\d+)?)\s*x\s*(\d+(?:\.\d+)?)\s+pattern",
    re.IGNORECASE,
)


def _thread_diameter(thread: str) -> float:
    """Extract nominal diameter from thread string, e.g. 'M3' -> 3.0, 'M2.5' -> 2.5."""
    return float(thread[1:])


def parse_interface(s: str) -> ParsedInterface | None:
    """Parse a free-text interface string into structured hole parameters.

    Returns ``None`` if the string does not match the expected pattern.
    """
    m = _INTERFACE_RE.search(s)
    if m is None:
        return None

    count = int(m.group(1)) if m.group(1) else 1
    thread = m.group(2).upper()
    # Normalise to title-case thread: M2.5, M3, M10
    thread = "M" + thread[1:]
    hole_type = m.group(3).lower()
    remainder = (m.group(4) or "").strip()

    if hole_type == "heat-set":
        diameter = HEAT_SET_DIAMETERS.get(thread)
        if diameter is None:
            diameter = _thread_diameter(thread) + 1.0
    elif hole_type == "press-fit":
        diameter = _thread_diameter(thread)
    elif hole_type in ("clearance", "through"):
        diameter = CLEARANCE_DIAMETERS.get(thread)
        if diameter is None:
            diameter = _thread_diameter(thread) + 0.4
    else:
        diameter = _thread_diameter(thread)

    pattern_x: float | None = None
    pattern_y: float | None = None
    pm = _PATTERN_HINT_RE.search(remainder)
    if pm:
        pattern_x = float(pm.group(1))
        pattern_y = float(pm.group(2))

    return ParsedInterface(
        count=count,
        thread=thread,
        hole_type=hole_type,
        diameter_mm=diameter,
        pattern_x=pattern_x,
        pattern_y=pattern_y,
        remainder=remainder,
    )


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def to_base64(path: Path) -> str:
    """Read a file and return its content as a base64-encoded string."""
    return base64.b64encode(path.read_bytes()).decode("ascii")


def normalize_step_timestamp(path: Path, ts: str) -> None:
    """Replace the FILE_NAME timestamp in a STEP file with *ts* for determinism."""
    text = path.read_text(encoding="utf-8")
    # STEP FILE_NAME line contains a timestamp like '2026-02-10T12:34:56'
    text = re.sub(
        r"(FILE_NAME\s*\([^,]*,\s*')[^']*(')",
        rf"\g<1>{ts}\2",
        text,
    )
    path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def generate(
    spec: dict[str, Any],
    output_format: str,
    output_path: Path | None = None,
    options: dict[str, Any] | None = None,
) -> GenerateResult:
    """Dispatch CAD generation to the appropriate process-specific generator."""
    if not CAD_AVAILABLE:
        raise RuntimeError(
            "CAD generation requires cadquery. "
            "Install with: pip install -e ."
        )

    opts = options or {}

    meta = spec.get("meta", {})
    process = meta.get("process")

    # Lazy import to avoid pulling cadquery at module load
    if process in ("cnc", "print_3d"):
        from server.cad_gen_box import BoxCadGenerator

        gen: CadGenerator = BoxCadGenerator()
    else:
        raise ValueError(f"No CAD generator for process: {process!r}")

    if output_format not in gen.supported_formats():
        raise ValueError(
            f"Unsupported format {output_format!r} for process {process!r}. "
            f"Supported: {gen.supported_formats()}"
        )

    if output_path is None:
        spec_hash = meta.get("coverage_score", "unknown")
        # Use a deterministic temp subdir so repeated calls overwrite
        tmp = Path(tempfile.gettempdir()) / "mcp-spec-cad" / str(spec_hash)
        tmp.mkdir(parents=True, exist_ok=True)
        part_name = spec.get("part", {}).get("name", "part")
        safe_name = re.sub(r"[^\w\-]", "_", part_name).strip("_") or "part"
        ext = {"step": ".step", "stl": ".stl", "freecad": ".FCStd"}.get(output_format, ".step")
        output_path = tmp / f"{safe_name}{ext}"

    return gen.generate(spec, output_format, output_path, opts)
