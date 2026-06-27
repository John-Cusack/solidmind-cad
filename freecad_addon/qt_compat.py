"""Qt compatibility — resolve PySide2 vs PySide6 once, re-export."""

from __future__ import annotations

try:
    from PySide2.QtCore import QTimer  # type: ignore[import-untyped]
    from PySide2.QtWidgets import QApplication  # type: ignore[import-untyped]
except ImportError:
    from PySide6.QtCore import QTimer  # type: ignore[import-untyped]
    from PySide6.QtWidgets import QApplication  # type: ignore[import-untyped]

__all__ = ["QApplication", "QTimer"]
