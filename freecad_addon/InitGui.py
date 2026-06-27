"""FreeCAD auto-start hook for SolidMind CAD addon.

When this package is installed as a FreeCAD Mod (symlinked into
~/.local/share/FreeCAD/Mod/), FreeCAD will exec this file on startup,
automatically starting the SolidMind socket server.
"""

import sys
from pathlib import Path

# FreeCAD execs InitGui.py (no __file__), so resolve the symlink directly.
_mod_dir = Path.home() / ".local" / "share" / "FreeCAD" / "Mod" / "SolidMind"
_repo_root = str(_mod_dir.resolve().parent)
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

try:
    import freecad_addon

    freecad_addon.start()
    FreeCAD.Console.PrintMessage("[SolidMind] Addon started successfully\n")
except Exception as exc:
    import traceback

    FreeCAD.Console.PrintError(f"[SolidMind] Failed to start: {exc}\n")
    FreeCAD.Console.PrintError(traceback.format_exc() + "\n")
