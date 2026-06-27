"""FreeCAD selection observer for the SolidMind CAD addon.

Registers with FreeCADGui.Selection to track what the user clicks on
in the 3D view.  The stored selection is queryable via the ``get_selection``
command in commands.py.
"""

from __future__ import annotations

import logging
import os
from typing import Any

try:
    import FreeCADGui  # type: ignore[import-untyped]
except ImportError:
    FreeCADGui = None  # type: ignore[assignment]

log = logging.getLogger("solidmind.selection_observer")

_TOOL_LOG = bool(os.environ.get("SOLIDMIND_TOOL_LOG", ""))


class SelectionObserver:
    """Observes selection changes and stores the latest selection state.

    FreeCAD calls ``addSelection`` / ``removeSelection`` / ``clearSelection``
    on registered observers whenever the user clicks geometry.
    """

    def __init__(self) -> None:
        self._selections: list[dict[str, Any]] = []
        self._active = False

    def start(self) -> None:
        """Register with FreeCAD's selection system."""
        if FreeCADGui is None:
            return
        if not self._active:
            FreeCADGui.Selection.addObserver(self)
            self._active = True

    def stop(self) -> None:
        """Unregister from FreeCAD's selection system."""
        if FreeCADGui is None:
            return
        if self._active:
            FreeCADGui.Selection.removeObserver(self)
            self._active = False

    @property
    def is_active(self) -> bool:
        return self._active

    def get_current(self) -> list[dict[str, Any]]:
        """Return a snapshot of the current selections."""
        return list(self._selections)

    # -- FreeCAD observer callbacks --

    def addSelection(self, doc: str, obj: str, sub: str, pos: tuple[float, float, float]) -> None:  # noqa: N802
        """Called when the user selects a sub-element."""
        if _TOOL_LOG:
            log.debug("addSelection doc=%s obj=%s sub=%s pos=%s", doc, obj, sub, pos)
        self._selections.append(
            {
                "doc": doc,
                "object": obj,
                "sub_element": sub,
                "position": list(pos),
            }
        )

    def removeSelection(self, doc: str, obj: str, sub: str) -> None:  # noqa: N802
        """Called when the user deselects a sub-element."""
        if _TOOL_LOG:
            log.debug("removeSelection doc=%s obj=%s sub=%s", doc, obj, sub)
        self._selections = [
            s
            for s in self._selections
            if not (s["doc"] == doc and s["object"] == obj and s["sub_element"] == sub)
        ]

    def clearSelection(self, doc: str) -> None:  # noqa: N802
        """Called when selection is completely cleared."""
        if _TOOL_LOG:
            log.debug("clearSelection doc=%s", doc)
        self._selections.clear()

    def setSelection(self, doc: str) -> None:  # noqa: N802
        """Called when the full selection is replaced."""
        # Re-read from FreeCADGui.Selection
        self._selections.clear()


# Module-level singleton
_observer: SelectionObserver | None = None


def get_observer() -> SelectionObserver:
    """Get or create the global selection observer."""
    global _observer
    if _observer is None:
        _observer = SelectionObserver()
    return _observer
