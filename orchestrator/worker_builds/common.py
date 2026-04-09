"""Shared helpers for ``orchestrator.worker_builds.*`` builder modules.

Three responsibilities:

1. **Probe FreeCAD availability.** ``freecad_ready()`` and
   ``freecad_ready_with_import_step()`` answer "can I run a real build
   right now?" so tests can skip cleanly when the addon isn't running.

2. **Provide a minimal ``task`` stub.** The ``_build_*`` helpers in
   ``orchestrator.worker_entry`` expect a ``task`` argument with a
   ``progress`` list attribute (used only for logging). ``TaskStub``
   mimics the shape without requiring the full ``A2ATask`` class.

3. **Run synchronous builds from sync test code.** ``build_geometry()``
   is a thin wrapper around ``orchestrator.worker_entry._build_geometry``
   that fills in the thread/event-loop plumbing so pytest callers can
   just write ``step_path = build_geometry(sub_spec, output_dir)``.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("solidmind.worker_builds.common")

DEFAULT_FC_HOST = "127.0.0.1"
DEFAULT_FC_PORT = 9876


def fc_host() -> str:
    """Return the FreeCAD host to connect to (env-overridable)."""
    return os.environ.get("FREECAD_HOST", DEFAULT_FC_HOST)


def fc_port() -> int:
    """Return the FreeCAD port to connect to (env-overridable)."""
    return int(os.environ.get("FREECAD_PORT", str(DEFAULT_FC_PORT)))


# ---------------------------------------------------------------------------
# Task progress stub
# ---------------------------------------------------------------------------


@dataclass
class TaskStub:
    """Minimal shape-compatible stand-in for A2ATask.

    The ``_build_*`` helpers in ``orchestrator.worker_entry`` only read
    ``task.progress`` (and append to it). Nothing else is touched. This
    stub satisfies that interface for in-process test callers without
    pulling in the A2A server or asyncio machinery.
    """
    progress: list[str] = field(default_factory=list)

    def log(self) -> None:
        """Emit the accumulated progress lines at INFO level."""
        for line in self.progress:
            logger.info("worker_build: %s", line)


# ---------------------------------------------------------------------------
# FreeCAD readiness probes
# ---------------------------------------------------------------------------


def freecad_ready(
    host: str | None = None,
    port: int | None = None,
    timeout: float = 2.0,
) -> bool:
    """Return True if a FreeCAD addon is answering on the given socket.

    Probes via ``FreeCADClient.ping()`` — connects, sends a ping, checks
    the response. Safe to call from test skipUnless decorators; never
    raises, returns False on any failure.
    """
    from server.freecad_client import FreeCADClient, FreeCADConnectionError

    h = host or fc_host()
    p = port or fc_port()
    try:
        client = FreeCADClient(host=h, port=p)
        client.connect(timeout=timeout)
        try:
            return client.ping()
        finally:
            client.disconnect()
    except (FreeCADConnectionError, OSError, Exception) as exc:
        logger.debug("freecad_ready: probe failed for %s:%d: %s", h, p, exc)
        return False


def freecad_ready_with_import_step(
    host: str | None = None,
    port: int | None = None,
    timeout: float = 2.0,
) -> bool:
    """Return True only if the addon *also* has the ``import_step`` command.

    ``import_step`` was added in commit 36bd03e and a running FreeCAD
    instance that was started before that commit (or hasn't reloaded
    the addon) will answer ``freecad_ready()`` but fail on
    ``import_step`` with an "unknown command" error. Verify-mode tests
    need to skip in that case rather than fail confusingly.
    """
    from server.freecad_client import (
        FreeCADClient,
        FreeCADCommandError,
        FreeCADConnectionError,
    )

    h = host or fc_host()
    p = port or fc_port()
    try:
        client = FreeCADClient(host=h, port=p)
        client.connect(timeout=timeout)
        try:
            # Call with a path that will fail (doesn't exist) but should fail
            # with a "file not found" error, NOT an "unknown command" error.
            # If the command isn't registered, the addon returns a specific
            # error code we can detect.
            try:
                client.send_command(
                    "import_step",
                    timeout=5.0,
                    path="/tmp/__solidmind_probe_nonexistent__.step",
                )
                # Unexpected success — probe file doesn't exist, so a real
                # import_step would have raised. But a lenient implementation
                # might return ok=True anyway. Treat as available.
                return True
            except FreeCADCommandError as exc:
                msg = str(exc).lower()
                # "unknown command" / "no such command" => not registered
                if "unknown command" in msg or "no such" in msg or "not found" in msg and "step" not in msg:
                    return False
                # Any other error (FILE_NOT_FOUND, IMPORT_FAILED, etc.)
                # means the command is registered and ran. Good enough.
                return True
        finally:
            client.disconnect()
    except (FreeCADConnectionError, OSError) as exc:
        logger.debug(
            "freecad_ready_with_import_step: probe failed for %s:%d: %s",
            h, p, exc,
        )
        return False


# ---------------------------------------------------------------------------
# Build dispatch
# ---------------------------------------------------------------------------


def build_geometry(
    sub_spec: dict[str, Any],
    output_dir: Path,
    interfaces: list[dict[str, Any]] | None = None,
    part_name: str | None = None,
) -> Path:
    """Dispatch a synchronous build via ``worker_entry._build_geometry``.

    Parameters
    ----------
    sub_spec:
        The worker sub-spec. Must contain ``build_type`` (``envelope`` /
        ``gear`` / ``ring_gear`` / ``carrier``) and whatever fields that
        build type requires (see ``worker_entry.py``).
    output_dir:
        Directory to write STEP / STL / metadata into. Created if missing.
    interfaces:
        Optional list of interface dicts (used by the envelope path to
        produce ``interface_actuals``). Gear/carrier/ring_gear paths do
        their own measurement so this is ignored there.
    part_name:
        Override for the subsystem / output filename. Defaults to
        ``sub_spec["name"]`` or ``sub_spec["subsystem"]``.

    Returns
    -------
    Path
        Path to the written ``{part_name}.step`` file.

    Raises
    ------
    RuntimeError
        If the FreeCAD addon isn't reachable.
    FileNotFoundError
        If the STEP file wasn't produced (indicates a build-time error).
    """
    from orchestrator.worker_entry import _build_geometry

    if not freecad_ready():
        raise RuntimeError(
            f"FreeCAD addon not reachable at {fc_host()}:{fc_port()}. "
            "Start it with `scripts/install_freecad_addon.sh` and launch "
            "FreeCAD, or set FREECAD_HOST / FREECAD_PORT env vars."
        )

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    name = (
        part_name
        or sub_spec.get("name")
        or sub_spec.get("subsystem")
        or "part"
    )
    envelope = sub_spec.get("envelope_mm", [20, 20, 10])
    task = TaskStub()

    _build_geometry(
        fc_port=fc_port(),
        part_name=name,
        envelope=envelope,
        output_dir=output_dir,
        sub_spec=sub_spec,
        interfaces=interfaces or [],
        task=task,
    )
    task.log()

    step_path = output_dir / f"{name}.step"
    if not step_path.exists():
        raise FileNotFoundError(
            f"Build completed but STEP file missing: {step_path}. "
            f"Progress: {task.progress}"
        )
    return step_path


# ---------------------------------------------------------------------------
# Metadata helpers
# ---------------------------------------------------------------------------


def read_metadata(output_dir: Path) -> dict[str, Any]:
    """Read ``metadata.json`` from a worker output directory."""
    path = Path(output_dir) / "metadata.json"
    if not path.exists():
        raise FileNotFoundError(f"No metadata.json in {output_dir}")
    return json.loads(path.read_text())


def override_claimed_measurements(
    output_dir: Path,
    overrides: dict[str, dict[str, float]],
) -> dict[str, Any]:
    """Overwrite ``interface_actuals`` in an existing ``metadata.json``.

    Used by the drift test: after a real build, stomp the claimed values
    so they disagree with the actual geometry, then assert the verify-
    mode path catches the drift.
    """
    metadata = read_metadata(output_dir)
    metadata["interface_actuals"] = overrides
    path = Path(output_dir) / "metadata.json"
    path.write_text(json.dumps(metadata, indent=2))
    return metadata
