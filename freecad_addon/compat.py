"""FreeCAD compatibility layer — version detection, feature probing, safe property access.

Probes FreeCAD once at import time and exports stable APIs that abstract away
version differences between FreeCAD 0.21 and 1.0+.  Follows the same pattern
as ``freecad_addon/qt_compat.py``.
"""
from __future__ import annotations

import logging
from typing import Any

import FreeCAD  # type: ignore[import-untyped]

logger = logging.getLogger("solidmind.compat")

# ---------------------------------------------------------------------------
# Version detection
# ---------------------------------------------------------------------------

def _parse_version() -> tuple[int, int]:
    """Parse FreeCAD.Version() into a (major, minor) tuple."""
    try:
        v = FreeCAD.Version()
        # v is a list: ['0', '21', '...'] or ['1', '0', '...']
        return (int(v[0]), int(v[1]))
    except Exception:
        logger.warning("Could not parse FreeCAD version, assuming (0, 21)")
        return (0, 21)


VERSION_TUPLE: tuple[int, int] = _parse_version()
IS_V1_PLUS: bool = VERSION_TUPLE >= (1, 0)
IS_V1_1_PLUS: bool = VERSION_TUPLE >= (1, 1)

logger.info(
    "FreeCAD version: %d.%d (IS_V1_PLUS=%s, IS_V1_1_PLUS=%s)",
    VERSION_TUPLE[0], VERSION_TUPLE[1], IS_V1_PLUS, IS_V1_1_PLUS,
)


# ---------------------------------------------------------------------------
# Sketch support property
# ---------------------------------------------------------------------------

def set_sketch_support(sketch: Any, support: Any, map_mode: str = "FlatFace") -> None:
    """Set sketch attachment support, handling property name differences.

    FreeCAD 1.0+ uses ``AttachmentSupport``; older versions used ``Support``.
    """
    if hasattr(sketch, "AttachmentSupport"):
        sketch.AttachmentSupport = support
    elif hasattr(sketch, "Support"):
        sketch.Support = support
    else:
        raise AttributeError(
            f"Sketch '{getattr(sketch, 'Name', '?')}' has neither "
            f"'AttachmentSupport' nor 'Support' property"
        )
    sketch.MapMode = map_mode


# ---------------------------------------------------------------------------
# Assembly module imports
# ---------------------------------------------------------------------------

def get_assembly_modules() -> tuple[Any, Any]:
    """Import JointObject + UtilsAssembly with workbench activation fallback.

    Returns (JointObject, UtilsAssembly) or raises ImportError with a clear message.
    """
    try:
        import JointObject  # type: ignore[import-untyped]
        import UtilsAssembly  # type: ignore[import-untyped]
        return JointObject, UtilsAssembly
    except ImportError:
        pass

    # Workbench may not have added its path yet — try activating it
    try:
        import FreeCADGui  # type: ignore[import-untyped]
        if FreeCADGui is not None:
            FreeCADGui.activateWorkbench("AssemblyWorkbench")
        import JointObject  # type: ignore[import-untyped]
        import UtilsAssembly  # type: ignore[import-untyped]
        return JointObject, UtilsAssembly
    except (ImportError, Exception) as exc:
        raise ImportError(
            f"Assembly workbench modules (JointObject, UtilsAssembly) not available. "
            f"FreeCAD version: {VERSION_TUPLE[0]}.{VERSION_TUPLE[1]}. "
            f"Assembly features require FreeCAD 1.0+. "
            f"Tier 1 analytical validation works without Assembly. "
            f"Original error: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Object lookup
# ---------------------------------------------------------------------------

def find_object(doc: Any, name: str, search_groups: bool = True) -> Any | None:
    """Look up an object by name with fallback strategies.

    1. Try ``doc.getObject(name)`` directly
    2. Try common suffixes (``001``, ``002``, ``003``)
    3. If ``search_groups`` is True, walk all Group containers and match by
       Name or Label
    """
    # Direct lookup
    obj = doc.getObject(name)
    if obj is not None:
        return obj

    # Try common suffixes
    for suffix in ("001", "002", "003"):
        obj = doc.getObject(f"{name}{suffix}")
        if obj is not None:
            logger.info("find_object: '%s' found as '%s' (suffix fallback)", name, obj.Name)
            return obj

    if not search_groups:
        return None

    # Walk all objects — match by Name or Label
    for obj in doc.Objects:
        if obj.Name == name or obj.Label == name:
            return obj
        # Search inside Group containers (e.g. JointGroup)
        if hasattr(obj, "Group"):
            for child in obj.Group:
                if child.Name == name or child.Label == name:
                    logger.info(
                        "find_object: '%s' found inside group '%s'",
                        name, obj.Name,
                    )
                    return child

    return None


def find_joint_in_assembly(doc: Any, asm_obj: Any, name: str) -> Any | None:
    """Find a joint object inside an assembly by name.

    1. Walk assembly's Group → find JointGroup → search children
    2. Try UtilsAssembly.getJointGroup as backup (log failures)
    3. Last resort: doc-wide getObject (ambiguous with multiple assemblies)
    """
    # 1. Walk assembly's Group directly (no fragile imports needed)
    if hasattr(asm_obj, "Group"):
        for child in asm_obj.Group:
            if hasattr(child, "Group"):
                for gchild in child.Group:
                    if gchild.Name == name or gchild.Label == name:
                        logger.info(
                            "find_joint: '%s' found via Group traversal as '%s'",
                            name, gchild.Name,
                        )
                        return gchild

    # 2. Try UtilsAssembly as backup, LOG failures
    try:
        _JointObject, UtilsAssembly = get_assembly_modules()
        joint_group = UtilsAssembly.getJointGroup(asm_obj)
        if joint_group is not None and hasattr(joint_group, "Group"):
            for child in joint_group.Group:
                if child.Name == name or child.Label == name:
                    logger.info(
                        "find_joint: '%s' found in JointGroup as '%s'",
                        name, child.Name,
                    )
                    return child
    except Exception as exc:
        logger.warning("UtilsAssembly.getJointGroup failed: %s", exc)

    # 3. Last resort: doc-wide lookup (ambiguous with multiple assemblies)
    return doc.getObject(name)


# ---------------------------------------------------------------------------
# Safe property access
# ---------------------------------------------------------------------------

def set_property_safe(obj: Any, primary: str, value: Any, fallbacks: list[str] | None = None) -> bool:
    """Set a property on obj, trying primary name first then fallbacks.

    Returns True if set successfully, False if no matching property found.
    """
    if hasattr(obj, primary):
        setattr(obj, primary, value)
        return True
    for alt in (fallbacks or []):
        if hasattr(obj, alt):
            logger.info("set_property_safe: '%s' not found, using fallback '%s'", primary, alt)
            setattr(obj, alt, value)
            return True
    return False


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

def list_objects_like(doc: Any, pattern: str) -> list[dict[str, str]]:
    """List objects whose Name or TypeId contains ``pattern`` (case-insensitive).

    Returns a list of ``{"name": ..., "type": ..., "label": ...}`` dicts.
    """
    pattern_lower = pattern.lower()
    results: list[dict[str, str]] = []
    for obj in doc.Objects:
        if (pattern_lower in obj.Name.lower()
                or pattern_lower in obj.TypeId.lower()
                or pattern_lower in obj.Label.lower()):
            results.append({
                "name": obj.Name,
                "type": obj.TypeId,
                "label": obj.Label,
            })
    return results


def probe_modules() -> dict[str, bool]:
    """Probe availability of key FreeCAD modules.

    Returns a dict of module_name -> available (bool).
    """
    modules = [
        "Sketcher", "Part", "PartDesign",
        "JointObject", "UtilsAssembly", "pivy.coin",
    ]
    results: dict[str, bool] = {}
    for mod in modules:
        try:
            __import__(mod)
            results[mod] = True
        except ImportError:
            results[mod] = False
    return results


def get_qt_backend() -> str:
    """Return the Qt backend in use (PySide2 or PySide6)."""
    try:
        import PySide2  # type: ignore[import-untyped]  # noqa: F401
        return "PySide2"
    except ImportError:
        pass
    try:
        import PySide6  # type: ignore[import-untyped]  # noqa: F401
        return "PySide6"
    except ImportError:
        return "unknown"


def get_workbenches() -> list[str]:
    """Return list of available workbenches, or empty list if FreeCADGui unavailable."""
    try:
        import FreeCADGui  # type: ignore[import-untyped]
        if FreeCADGui is not None and hasattr(FreeCADGui, "listWorkbenches"):
            return list(FreeCADGui.listWorkbenches().keys())
    except (ImportError, Exception):
        pass
    return []


def freecad_info() -> dict[str, Any]:
    """Return comprehensive FreeCAD runtime environment information."""
    return {
        "version": list(VERSION_TUPLE),
        "is_v1_plus": IS_V1_PLUS,
        "workbenches": get_workbenches(),
        "modules": probe_modules(),
        "qt_backend": get_qt_backend(),
    }


# ---------------------------------------------------------------------------
# Version guards
# ---------------------------------------------------------------------------

def require_v1_plus(feature_name: str = "Assembly features") -> None:
    """Raise RuntimeError if FreeCAD version is below 1.0.

    Provides a clear, actionable error message.
    """
    if not IS_V1_PLUS:
        raise RuntimeError(
            f"{feature_name} require FreeCAD 1.0+. "
            f"Current version: {VERSION_TUPLE[0]}.{VERSION_TUPLE[1]}. "
            f"Tier 1 analytical validation works without Assembly."
        )
