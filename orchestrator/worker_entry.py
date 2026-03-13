"""Worker container entry point — starts FreeCAD + A2A server.

This runs inside a Docker container. It:
1. Starts FreeCAD headless with the addon (socket server on port 9876)
2. Waits for FreeCAD to be ready (polls ping)
3. Starts the A2A HTTP server (port 8080) with a build_fn that
   drives FreeCAD via socket commands

Usage:
    python -m orchestrator.worker_entry
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# FreeCAD process management
# ---------------------------------------------------------------------------

_freecad_proc: subprocess.Popen | None = None


def _find_freecad(*, headless: bool = True) -> str:
    """Locate the FreeCAD binary (extracted AppImage or system install).

    In headless mode, prefer FreeCADCmd (no Qt) to avoid QTimer issues
    where the main thread is blocked and commands time out.
    """
    fc_path = os.environ.get("FREECAD_PATH", "/opt/freecad")

    if headless:
        # Prefer FreeCADCmd — no Qt, socket server uses direct dispatch
        candidates = [
            f"{fc_path}/usr/bin/FreeCADCmd",
            f"{fc_path}/usr/bin/freecadcmd",
        ]
    else:
        candidates = [
            f"{fc_path}/AppRun",
        ]

    # Add generic fallbacks
    candidates.extend([
        f"{fc_path}/AppRun",
        f"{fc_path}/usr/bin/FreeCADCmd",
    ])

    for c in candidates:
        if c and Path(c).exists():
            return c

    # Try PATH
    names = ["FreeCADCmd", "freecadcmd"] if headless else ["FreeCAD", "freecad"]
    names += ["FreeCADCmd", "FreeCAD"]
    for name in names:
        found = shutil.which(name)
        if found:
            return found

    raise FileNotFoundError(
        "FreeCAD binary not found. Set FREECAD_PATH or install FreeCAD."
    )


def start_freecad(
    *,
    host: str = "0.0.0.0",
    port: int = 9876,
    use_xvfb: bool = False,
) -> subprocess.Popen:
    """Launch FreeCAD headless with the addon socket server.

    Uses AppRun + FreeCADCmd for headless mode (no Qt event loop).
    This avoids QTimer issues where the main thread is blocked.
    """
    global _freecad_proc

    fc_path = os.environ.get("FREECAD_PATH", "/opt/freecad")
    apprun = Path(fc_path) / "AppRun"
    startup_script = str(Path(__file__).parent.parent / "docker" / "freecad_startup.py")

    env = os.environ.copy()
    env["FREECAD_HOST"] = host
    env["FREECAD_PORT"] = str(port)

    if apprun.exists():
        # Use AppRun + FreeCADCmd for headless operation
        # SOLIDMIND_HEADLESS=1 in the startup script forces direct dispatch
        # (no QTimer), avoiding GIL contention with Qt's event loop.
        fc_cmd = [str(apprun), "FreeCADCmd", startup_script]
    else:
        # System FreeCAD
        binary = _find_freecad(headless=True)
        fc_cmd = [binary, startup_script]

    if use_xvfb:
        cmd = ["xvfb-run", "-a", "--server-args=-screen 0 1024x768x24"] + fc_cmd
    else:
        cmd = fc_cmd

    log.info("Starting FreeCAD: %s", " ".join(cmd))
    _freecad_proc = subprocess.Popen(
        cmd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return _freecad_proc


def wait_for_freecad(
    host: str = "127.0.0.1",
    port: int = 9876,
    timeout: float = 120.0,
    poll_interval: float = 2.0,
) -> None:
    """Poll until FreeCAD addon responds to ping on the socket."""
    deadline = time.monotonic() + timeout
    attempt = 0

    while time.monotonic() < deadline:
        attempt += 1
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(3.0)
            sock.connect((host, port))
            # Send ping command
            sock.sendall(b'{"cmd":"ping","args":{}}\n')
            data = sock.recv(4096)
            sock.close()
            if b"pong" in data:
                log.info("FreeCAD ready after %d attempts", attempt)
                return
        except (ConnectionRefusedError, OSError, socket.timeout):
            pass
        finally:
            try:
                sock.close()
            except Exception:
                pass

        # Check if FreeCAD process died
        if _freecad_proc and _freecad_proc.poll() is not None:
            rc = _freecad_proc.returncode
            stderr = _freecad_proc.stderr.read().decode("utf-8", errors="replace") if _freecad_proc.stderr else ""
            raise RuntimeError(
                f"FreeCAD exited with code {rc} before becoming ready.\n"
                f"stderr: {stderr[:2000]}"
            )

        log.debug("FreeCAD not ready (attempt %d), waiting %.0fs...", attempt, poll_interval)
        time.sleep(poll_interval)

    raise TimeoutError(f"FreeCAD did not become ready within {timeout}s")


def stop_freecad() -> None:
    """Terminate the FreeCAD process."""
    global _freecad_proc
    if _freecad_proc and _freecad_proc.poll() is None:
        log.info("Stopping FreeCAD (pid=%d)", _freecad_proc.pid)
        _freecad_proc.terminate()
        try:
            _freecad_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _freecad_proc.kill()
    _freecad_proc = None


# ---------------------------------------------------------------------------
# FreeCAD readiness flag (for health endpoint)
# ---------------------------------------------------------------------------

_freecad_ready = False


def is_freecad_ready() -> bool:
    return _freecad_ready


# ---------------------------------------------------------------------------
# Build function — drives FreeCAD via socket to build geometry
# ---------------------------------------------------------------------------


_LONG_COMMANDS = frozenset({
    "polar_pattern", "linear_pattern", "export",
    "pad", "pocket", "sketch_populate",
})


def _send(host: str, port: int, cmd: str, **args: Any) -> dict[str, Any]:
    """Send a command to FreeCAD socket server and return the result."""
    from server.freecad_client import FreeCADClient

    client = FreeCADClient(host=host, port=port)
    client.connect(timeout=5.0)
    try:
        cmd_timeout = 300.0 if cmd in _LONG_COMMANDS else 60.0
        return client.send_command(cmd, timeout=cmd_timeout, **args)
    finally:
        client.disconnect()


async def build_from_spec(
    sub_spec: dict[str, Any],
    interfaces: list[dict[str, Any]],
    task: Any,
) -> dict[str, Any]:
    """Build geometry from a sub-spec using FreeCAD socket commands.

    This is the build_fn passed to the A2A server. It translates the
    sub-spec into direct FreeCAD commands (no LLM needed).

    For production use with Claude Code, replace this with a function
    that launches `claude --print` with the appropriate prompt.
    """
    fc_host = os.environ.get("FREECAD_HOST", "127.0.0.1")
    # Use localhost to connect (even if server binds 0.0.0.0)
    fc_port = int(os.environ.get("FREECAD_PORT", "9876"))

    part_name = sub_spec.get("name", "part")
    envelope = sub_spec.get("envelope_mm", [20, 20, 10])
    output_dir = Path(os.environ.get("OUTPUT_DIR", "/output")) / part_name
    output_dir.mkdir(parents=True, exist_ok=True)

    task.progress.append(f"Starting build: {part_name}")

    # Run blocking FreeCAD commands in executor to not block event loop
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None,
        _build_geometry,
        fc_port, part_name, envelope, output_dir, sub_spec, interfaces, task,
    )
    return result


def _build_geometry(
    fc_port: int,
    part_name: str,
    envelope: list[float],
    output_dir: Path,
    sub_spec: dict[str, Any],
    interfaces: list[dict[str, Any]],
    task: Any,
) -> dict[str, Any]:
    """Synchronous geometry build — runs in thread executor."""
    host = "127.0.0.1"

    # Route to specialized builder if sub_spec has a build_type
    build_type = sub_spec.get("build_type", "envelope")
    if build_type == "gear":
        return _build_gear(host, fc_port, part_name, output_dir, sub_spec, task)
    elif build_type == "ring_gear":
        return _build_ring_gear(host, fc_port, part_name, output_dir, sub_spec, task)
    elif build_type == "carrier":
        return _build_carrier(host, fc_port, part_name, output_dir, sub_spec, task)

    # Default: envelope box + interface features
    return _build_envelope(
        host, fc_port, part_name, envelope, output_dir, sub_spec, interfaces, task,
    )


def _export_and_package(
    host: str,
    fc_port: int,
    part_name: str,
    doc_name: str,
    body_name: str,
    output_dir: Path,
    sub_spec: dict[str, Any],
    task: Any,
) -> dict[str, Any]:
    """Common export + metadata + artifact packaging."""
    # Export STEP
    step_path = str(output_dir / f"{part_name}.step")
    _send(host, fc_port, "export", path=step_path, format="step", doc=doc_name)
    task.progress.append("STEP exported")

    # Export STL
    stl_path = str(output_dir / f"{part_name}.stl")
    _send(host, fc_port, "export", path=stl_path, format="stl", doc=doc_name)
    task.progress.append("STL exported")

    # Get dimensions
    try:
        dims = _send(host, fc_port, "get_dimensions", body=body_name, doc=doc_name)
    except Exception:
        dims = {}

    metadata = {
        "subsystem": part_name,
        "doc_name": doc_name,
        "body_name": body_name,
        "claimed_mass_kg": dims.get("mass_kg", 0.05),
        "claimed_bounding_box_mm": dims.get("bounding_box", sub_spec.get("envelope_mm", [])),
        "params": sub_spec.get("params", {}),
        "screenshots": [],
        "deviations": [],
        "notes": f"Docker worker build — {part_name}",
    }
    metadata_path = output_dir / "metadata.json"
    metadata_path.write_text(json.dumps(metadata, indent=2))
    task.progress.append("Metadata written")

    artifacts = []
    for fpath in [step_path, stl_path, str(metadata_path)]:
        p = Path(fpath)
        if p.exists():
            if p.suffix == ".json":
                artifacts.append({
                    "type": "data",
                    "name": p.name,
                    "data": json.loads(p.read_text()),
                })
            else:
                artifacts.append({
                    "type": "file",
                    "name": p.name,
                    "data": base64.b64encode(p.read_bytes()).decode(),
                })

    return {"artifacts": artifacts}


def _build_envelope(
    host: str,
    fc_port: int,
    part_name: str,
    envelope: list[float],
    output_dir: Path,
    sub_spec: dict[str, Any],
    interfaces: list[dict[str, Any]],
    task: Any,
) -> dict[str, Any]:
    """Build a box from envelope dims + interface features (original logic)."""
    # 1. New document
    doc_result = _send(host, fc_port, "new_document", name=part_name)
    doc_name = doc_result.get("name", part_name)
    task.progress.append(f"Document created: {doc_name}")

    # 2. New body
    body_result = _send(host, fc_port, "new_body", name=part_name, doc=doc_name)
    body_name = body_result["name"]
    task.progress.append(f"Body created: {body_name}")

    # 3. Sketch base rectangle on XY plane
    w, d, h = envelope[0], envelope[1], envelope[2]
    sk = _send(host, fc_port, "new_sketch", body=body_name, plane="XY", doc=doc_name)
    _send(host, fc_port, "sketch_populate", sketch=sk["sketch"], doc=doc_name, elements=[
        {"type": "rect", "x": -w / 2, "y": -d / 2, "w": w, "h": d},
    ])
    _send(host, fc_port, "close_sketch", sketch=sk["sketch"], doc=doc_name)
    task.progress.append("Base sketch complete")

    # 4. Pad the base
    _send(host, fc_port, "pad", sketch=sk["sketch"], length=h, doc=doc_name)
    task.progress.append(f"Padded {h}mm")

    # 5. Add interface features (boss or hole based on spec)
    for ifc in interfaces:
        geom = ifc.get("geometry", {})
        if geom.get("type") == "cylinder":
            diameter = geom.get("diameter_mm", 10)
            depth = geom.get("depth_mm", 5)
            is_boss = ifc.get("subsystem_a") == part_name or sub_spec.get("role") == "boss"

            sk2 = _send(host, fc_port, "new_sketch", body=body_name, plane="XY", doc=doc_name)
            _send(host, fc_port, "sketch_populate", sketch=sk2["sketch"], doc=doc_name, elements=[
                {"type": "circle", "cx": 0, "cy": 0, "r": diameter / 2},
            ])
            _send(host, fc_port, "close_sketch", sketch=sk2["sketch"], doc=doc_name)

            if is_boss:
                _send(host, fc_port, "pad", sketch=sk2["sketch"], length=depth, doc=doc_name)
                task.progress.append(f"Boss Ø{diameter}×{depth}")
            else:
                _send(host, fc_port, "pocket", sketch=sk2["sketch"], length=depth, doc=doc_name)
                task.progress.append(f"Pocket Ø{diameter}×{depth}")

    return _export_and_package(
        host, fc_port, part_name, doc_name, body_name, output_dir, sub_spec, task,
    )


def _build_gear(
    host: str,
    fc_port: int,
    part_name: str,
    output_dir: Path,
    sub_spec: dict[str, Any],
    task: Any,
) -> dict[str, Any]:
    """Build an external gear from pre-computed sketch elements.

    sub_spec fields:
      - sketch_elements: list of sketch elements (lines, arcs, splines)
      - thickness_mm: gear face width
      - bore_diameter_mm: central bore (optional)
      - params: gear parameters for metadata
    """
    thickness = sub_spec.get("thickness_mm", 8)
    bore_dia = sub_spec.get("bore_diameter_mm", 0)
    elements = sub_spec.get("sketch_elements", [])

    doc_result = _send(host, fc_port, "new_document", name=part_name)
    doc_name = doc_result.get("name", part_name)
    task.progress.append(f"Document created: {doc_name}")

    body_result = _send(host, fc_port, "new_body", name=part_name, doc=doc_name)
    body_name = body_result["name"]
    task.progress.append(f"Body created: {body_name}")

    # Sketch gear profile
    sk = _send(host, fc_port, "new_sketch", body=body_name, plane="XY", doc=doc_name)
    _send(host, fc_port, "sketch_populate", sketch=sk["sketch"], doc=doc_name,
          elements=elements)
    _send(host, fc_port, "close_sketch", sketch=sk["sketch"], doc=doc_name)
    task.progress.append(f"Gear profile sketched ({len(elements)} elements)")

    # Pad
    _send(host, fc_port, "pad", sketch=sk["sketch"], length=thickness, doc=doc_name)
    task.progress.append(f"Padded {thickness}mm")

    # Bore hole
    if bore_dia > 0:
        sk2 = _send(host, fc_port, "new_sketch", body=body_name, plane="XY", doc=doc_name)
        _send(host, fc_port, "sketch_populate", sketch=sk2["sketch"], doc=doc_name,
              elements=[{"type": "circle", "cx": 0, "cy": 0, "r": bore_dia / 2}])
        _send(host, fc_port, "close_sketch", sketch=sk2["sketch"], doc=doc_name)
        _send(host, fc_port, "pocket", sketch=sk2["sketch"],
              pocket_type="ThroughAll", reversed="auto", verify=False, doc=doc_name)
        task.progress.append(f"Bore Ø{bore_dia}")

    return _export_and_package(
        host, fc_port, part_name, doc_name, body_name, output_dir, sub_spec, task,
    )


def _build_ring_gear(
    host: str,
    fc_port: int,
    part_name: str,
    output_dir: Path,
    sub_spec: dict[str, Any],
    task: Any,
) -> dict[str, Any]:
    """Build a ring gear: blank disc → pocket tooth slot → polar pattern.

    sub_spec fields:
      - blank_elements: circle element for the outer blank
      - slot_elements: tooth slot profile elements
      - ring_teeth: number of teeth (for polar pattern)
      - thickness_mm: gear face width
    """
    thickness = sub_spec.get("thickness_mm", 8)
    blank_elements = sub_spec.get("blank_elements", [])
    slot_elements = sub_spec.get("slot_elements", [])
    ring_teeth = sub_spec.get("ring_teeth", 42)

    doc_result = _send(host, fc_port, "new_document", name=part_name)
    doc_name = doc_result.get("name", part_name)
    task.progress.append(f"Document created: {doc_name}")

    body_result = _send(host, fc_port, "new_body", name=part_name, doc=doc_name)
    body_name = body_result["name"]
    task.progress.append(f"Body created: {body_name}")

    # 1. Sketch + pad the outer blank
    sk = _send(host, fc_port, "new_sketch", body=body_name, plane="XY", doc=doc_name)
    _send(host, fc_port, "sketch_populate", sketch=sk["sketch"], doc=doc_name,
          elements=blank_elements)
    _send(host, fc_port, "close_sketch", sketch=sk["sketch"], doc=doc_name)
    _send(host, fc_port, "pad", sketch=sk["sketch"], length=thickness, doc=doc_name)
    task.progress.append("Ring blank padded")

    # 2. Sketch tooth slot on top face → pocket through all
    sk2 = _send(host, fc_port, "new_sketch", body=body_name, plane="XY", doc=doc_name)
    _send(host, fc_port, "sketch_populate", sketch=sk2["sketch"], doc=doc_name,
          elements=slot_elements)
    _send(host, fc_port, "close_sketch", sketch=sk2["sketch"], doc=doc_name)
    pocket_result = _send(host, fc_port, "pocket", sketch=sk2["sketch"],
                          pocket_type="ThroughAll", reversed="auto",
                          verify=False, doc=doc_name)
    pocket_name = pocket_result.get("name", "Pocket")
    task.progress.append("Tooth slot pocketed")

    # 3. Polar pattern to replicate around ring
    _send(host, fc_port, "polar_pattern", features=[pocket_name],
          occurrences=ring_teeth, axis="Base_Z", verify=False, doc=doc_name)
    task.progress.append(f"Polar pattern × {ring_teeth}")

    return _export_and_package(
        host, fc_port, part_name, doc_name, body_name, output_dir, sub_spec, task,
    )


def _build_carrier(
    host: str,
    fc_port: int,
    part_name: str,
    output_dir: Path,
    sub_spec: dict[str, Any],
    task: Any,
) -> dict[str, Any]:
    """Build a carrier plate: disc with planet pin bosses.

    sub_spec fields:
      - outer_radius_mm: carrier disc radius
      - thickness_mm: plate thickness
      - bore_diameter_mm: central bore
      - pin_positions: list of [x, y] for planet pin centers
      - pin_diameter_mm: planet pin diameter
      - pin_height_mm: pin boss height above plate
    """
    outer_r = sub_spec.get("outer_radius_mm", 25)
    thickness = sub_spec.get("thickness_mm", 4)
    bore_dia = sub_spec.get("bore_diameter_mm", 5)
    pin_positions = sub_spec.get("pin_positions", [])
    pin_dia = sub_spec.get("pin_diameter_mm", 5)
    pin_height = sub_spec.get("pin_height_mm", 6)

    doc_result = _send(host, fc_port, "new_document", name=part_name)
    doc_name = doc_result.get("name", part_name)
    task.progress.append(f"Document created: {doc_name}")

    body_result = _send(host, fc_port, "new_body", name=part_name, doc=doc_name)
    body_name = body_result["name"]
    task.progress.append(f"Body created: {body_name}")

    # 1. Base disc
    sk = _send(host, fc_port, "new_sketch", body=body_name, plane="XY", doc=doc_name)
    _send(host, fc_port, "sketch_populate", sketch=sk["sketch"], doc=doc_name,
          elements=[{"type": "circle", "cx": 0, "cy": 0, "r": outer_r}])
    _send(host, fc_port, "close_sketch", sketch=sk["sketch"], doc=doc_name)
    _send(host, fc_port, "pad", sketch=sk["sketch"], length=thickness, doc=doc_name)
    task.progress.append(f"Carrier disc Ø{outer_r * 2} × {thickness}mm")

    # 2. Central bore
    if bore_dia > 0:
        sk2 = _send(host, fc_port, "new_sketch", body=body_name, plane="XY", doc=doc_name)
        _send(host, fc_port, "sketch_populate", sketch=sk2["sketch"], doc=doc_name,
              elements=[{"type": "circle", "cx": 0, "cy": 0, "r": bore_dia / 2}])
        _send(host, fc_port, "close_sketch", sketch=sk2["sketch"], doc=doc_name)
        _send(host, fc_port, "pocket", sketch=sk2["sketch"],
              pocket_type="ThroughAll", reversed="auto", verify=False, doc=doc_name)
        task.progress.append(f"Bore Ø{bore_dia}")

    # 3. Planet pin bosses
    for i, pos in enumerate(pin_positions):
        sk3 = _send(host, fc_port, "new_sketch", body=body_name, plane="XY", doc=doc_name)
        _send(host, fc_port, "sketch_populate", sketch=sk3["sketch"], doc=doc_name,
              elements=[{"type": "circle", "cx": pos[0], "cy": pos[1], "r": pin_dia / 2}])
        _send(host, fc_port, "close_sketch", sketch=sk3["sketch"], doc=doc_name)
        _send(host, fc_port, "pad", sketch=sk3["sketch"], length=pin_height, doc=doc_name)
        task.progress.append(f"Pin boss {i + 1} at ({pos[0]:.1f}, {pos[1]:.1f})")

    return _export_and_package(
        host, fc_port, part_name, doc_name, body_name, output_dir, sub_spec, task,
    )


# ---------------------------------------------------------------------------
# Enhanced health check
# ---------------------------------------------------------------------------


def create_worker_app(
    *,
    worker_name: str = "solidmind-worker",
    worker_description: str = "CAD worker",
    build_fn: Any = None,
) -> Any:
    """Create the A2A app with an enhanced health endpoint."""
    from orchestrator.a2a_server import create_app

    def _health_check() -> dict[str, Any]:
        fc_ready = is_freecad_ready()
        return {
            "status": "ok" if fc_ready else "starting",
            "freecad": fc_ready,
        }

    return create_app(
        worker_name=worker_name,
        worker_description=worker_description,
        build_fn=build_fn or build_from_spec,
        health_fn=_health_check,
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Start the worker: FreeCAD + A2A server."""
    global _freecad_ready

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
    )

    worker_port = int(os.environ.get("WORKER_PORT", "8080"))
    fc_host = os.environ.get("FREECAD_HOST", "0.0.0.0")
    fc_port = int(os.environ.get("FREECAD_PORT", "9876"))
    worker_name = os.environ.get("WORKER_NAME", "solidmind-worker")
    use_xvfb = os.environ.get("SOLIDMIND_USE_XVFB", "").lower() in ("1", "true", "yes")

    # Handle shutdown gracefully
    def _shutdown(signum: int, frame: Any) -> None:
        log.info("Received signal %d, shutting down...", signum)
        stop_freecad()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # 1. Start FreeCAD
    log.info("Starting FreeCAD (host=%s, port=%d, xvfb=%s)", fc_host, fc_port, use_xvfb)
    try:
        start_freecad(host=fc_host, port=fc_port, use_xvfb=use_xvfb)
    except FileNotFoundError as e:
        log.error("Cannot start FreeCAD: %s", e)
        sys.exit(1)

    # 2. Wait for FreeCAD readiness
    log.info("Waiting for FreeCAD to become ready...")
    try:
        wait_for_freecad(host="127.0.0.1", port=fc_port, timeout=120.0)
        _freecad_ready = True
        log.info("FreeCAD is ready")
    except (TimeoutError, RuntimeError) as e:
        log.error("FreeCAD startup failed: %s", e)
        stop_freecad()
        sys.exit(1)

    # 3. Start A2A server (blocking)
    log.info("Starting A2A server on port %d", worker_port)
    import uvicorn

    app = create_worker_app(
        worker_name=worker_name,
        worker_description=f"CAD worker: {worker_name}",
    )
    try:
        uvicorn.run(app, host="0.0.0.0", port=worker_port, log_level="info")
    finally:
        stop_freecad()


if __name__ == "__main__":
    main()
