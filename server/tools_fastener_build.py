"""MCP tools for building bolt and nut geometry in FreeCAD.

One-call tools: cad.bolt builds a complete bolt, cad.nut builds a complete nut,
cad.find_holes identifies cylindrical holes and suggests bolt sizes.
"""
from __future__ import annotations

import math
import logging
from typing import Any

from server.fastener_data import (
    SUPPORTED_HEAD_TYPES,
    SUPPORTED_NUT_TYPES,
    SUPPORTED_SIZES,
    lookup,
    match_bolt_size,
    nut_lookup,
)
from server.freecad_client import FreeCADCommandError, FreeCADConnectionError, get_client

log = logging.getLogger("solidmind.tools_fastener_build")


def _error_result(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def _hex_elements(across_flats: float) -> list[dict[str, Any]]:
    """Generate 6 line elements forming a regular hexagon.

    Oriented with flats at top and bottom (vertices at 0, 60, ..., 300 deg).
    """
    r = across_flats / (2 * math.cos(math.radians(30)))  # across-corners / 2
    vertices = []
    for i in range(6):
        angle = math.radians(i * 60)
        vertices.append((round(r * math.cos(angle), 4),
                         round(r * math.sin(angle), 4)))

    lines: list[dict[str, Any]] = []
    for i in range(6):
        x1, y1 = vertices[i]
        x2, y2 = vertices[(i + 1) % 6]
        lines.append({"type": "line", "x1": x1, "y1": y1, "x2": x2, "y2": y2})
    return lines


def _placement_kwargs(
    position: list[float] | None,
    rotation_axis: list[float] | None,
    rotation_angle_deg: float,
    doc: str | None,
) -> dict[str, Any] | None:
    """Build set_placement kwargs if any placement is needed."""
    needs_placement = (
        position is not None
        or (rotation_axis is not None and rotation_angle_deg != 0.0)
    )
    if not needs_placement:
        return None

    kw: dict[str, Any] = {}
    if position is not None:
        kw["position"] = position
    if rotation_axis is not None:
        kw["rotation_axis"] = rotation_axis
        kw["rotation_angle_deg"] = rotation_angle_deg
    if doc is not None:
        kw["doc"] = doc
    return kw


def cad_bolt(
    size: str,
    length: float,
    head_type: str = "socket_head",
    position: list[float] | None = None,
    rotation_axis: list[float] | None = None,
    rotation_angle_deg: float = 0.0,
    name: str | None = None,
    verify: bool = True,
    doc: str | None = None,
) -> dict[str, Any]:
    """Build a complete bolt in FreeCAD.

    Creates a PartDesign body with head + shaft.  The bolt sits with its
    head top at z=head_height and shaft extending downward from z=0 to
    z=-length.  Hex heads use a proper hexagonal cross-section; all other
    head types use a cylindrical cross-section.

    Use ``rotation_axis`` + ``rotation_angle_deg`` to orient the bolt for
    non-vertical holes (e.g., rotation_axis=[1,0,0], rotation_angle_deg=90
    rotates the bolt to point along -Y instead of -Z).

    Parameters
    ----------
    size : str
        Metric size, e.g. "M4", "M8".
    length : float
        Shaft length in mm.
    head_type : str
        One of socket_head, hex, button_head, countersunk, set_screw.
    position : list[float] | None
        Optional [x, y, z] placement in mm.
    rotation_axis : list[float] | None
        Axis to rotate around, e.g. [1, 0, 0].
    rotation_angle_deg : float
        Rotation angle in degrees (default 0).
    name : str | None
        Body label. Defaults to "Bolt_M4_socket_head" etc.
    verify : bool
        Capture verification screenshots on last operation.
    doc : str | None
        Document name. Uses active document if None.
    """
    spec = lookup(size=size, length=length, head_type=head_type)
    if spec is None:
        return _error_result(
            "FASTENER_NOT_FOUND",
            f"No data for size='{size}' head_type='{head_type}'. "
            f"Supported sizes: {SUPPORTED_SIZES}. "
            f"Supported head types: {SUPPORTED_HEAD_TYPES}.",
        )

    body_name = name or f"Bolt_{size}_{head_type}"
    client = get_client()
    placement_kw = _placement_kwargs(position, rotation_axis, rotation_angle_deg, doc)

    try:
        # 1. Create body
        kw: dict[str, Any] = {"name": body_name}
        if doc is not None:
            kw["doc"] = doc
        body_result = client.send_command("new_body", **kw)
        actual_body = body_result.get("body", body_name)

        has_head = head_type != "set_screw" and spec.head_height > 0

        if has_head:
            # 2. Build head
            if head_type == "hex":
                head_elements = _hex_elements(spec.head_diameter)
            else:
                head_elements = [{"type": "circle", "cx": 0, "cy": 0,
                                  "r": spec.head_diameter / 2}]

            sk1 = client.send_command("new_sketch", body=actual_body,
                                      plane="XY", **({"doc": doc} if doc else {}))
            sketch1 = sk1["sketch"]
            client.send_command("sketch_populate", sketch=sketch1,
                                elements=head_elements, constraints=[])
            client.send_command("close_sketch", sketch=sketch1)
            client.send_command("pad", sketch=sketch1, length=spec.head_height,
                                verify=False, **({"doc": doc} if doc else {}))

        # 3. Build shaft
        shaft_elements = [{"type": "circle", "cx": 0, "cy": 0,
                           "r": spec.thread_diameter / 2}]

        sk2 = client.send_command("new_sketch", body=actual_body,
                                  plane="XY", **({"doc": doc} if doc else {}))
        sketch2 = sk2["sketch"]
        client.send_command("sketch_populate", sketch=sketch2,
                            elements=shaft_elements, constraints=[])
        client.send_command("close_sketch", sketch=sketch2)

        is_last = placement_kw is None
        pad_result = client.send_command(
            "pad", sketch=sketch2, length=length, reversed=True,
            verify=verify and is_last, **({"doc": doc} if doc else {}),
        )

        # 4. Position/rotate if needed
        if placement_kw is not None:
            placement_kw["object_name"] = actual_body
            client.send_command("set_placement", **placement_kw)

        result: dict[str, Any] = {
            "ok": True,
            "body": actual_body,
            "size": size,
            "head_type": head_type,
            "length_mm": length,
            "head_diameter_mm": spec.head_diameter,
            "head_height_mm": spec.head_height,
            "thread_diameter_mm": spec.thread_diameter,
        }
        if position is not None:
            result["position"] = position
        if rotation_axis is not None:
            result["rotation_axis"] = rotation_axis
            result["rotation_angle_deg"] = rotation_angle_deg
        # Forward verification images from last pad
        for key in ("verification_images", "verification_views",
                     "face_map", "operation_summary"):
            if key in pad_result:
                result[key] = pad_result[key]
        return result

    except FreeCADConnectionError as e:
        log.error("FAIL cad_bolt CONNECTION_ERROR: %s", e)
        return _error_result("CONNECTION_ERROR", str(e))
    except FreeCADCommandError as e:
        log.error("FAIL cad_bolt COMMAND_ERROR: %s", e)
        return _error_result("COMMAND_ERROR", str(e))


def cad_nut(
    size: str,
    nut_type: str = "hex",
    position: list[float] | None = None,
    rotation_axis: list[float] | None = None,
    rotation_angle_deg: float = 0.0,
    name: str | None = None,
    verify: bool = True,
    doc: str | None = None,
) -> dict[str, Any]:
    """Build a complete nut in FreeCAD.

    Creates a PartDesign body with a hexagonal prism and a center through-hole.
    The nut sits from z=0 to z=height.

    Parameters
    ----------
    size : str
        Metric size, e.g. "M4", "M8".
    nut_type : str
        One of hex, thin, nyloc.
    position : list[float] | None
        Optional [x, y, z] placement in mm.
    rotation_axis : list[float] | None
        Axis to rotate around, e.g. [1, 0, 0].
    rotation_angle_deg : float
        Rotation angle in degrees (default 0).
    name : str | None
        Body label. Defaults to "Nut_M4_hex" etc.
    verify : bool
        Capture verification screenshots on last operation.
    doc : str | None
        Document name. Uses active document if None.
    """
    spec = nut_lookup(size=size, nut_type=nut_type)
    if spec is None:
        return _error_result(
            "NUT_NOT_FOUND",
            f"No data for size='{size}' nut_type='{nut_type}'. "
            f"Supported sizes: {SUPPORTED_SIZES}. "
            f"Supported nut types: {SUPPORTED_NUT_TYPES}.",
        )

    body_name = name or f"Nut_{size}_{nut_type}"
    client = get_client()
    placement_kw = _placement_kwargs(position, rotation_axis, rotation_angle_deg, doc)

    try:
        # 1. Create body
        kw: dict[str, Any] = {"name": body_name}
        if doc is not None:
            kw["doc"] = doc
        body_result = client.send_command("new_body", **kw)
        actual_body = body_result.get("body", body_name)

        # 2. Hex prism
        hex_elements = _hex_elements(spec.across_flats)
        sk1 = client.send_command("new_sketch", body=actual_body,
                                  plane="XY", **({"doc": doc} if doc else {}))
        sketch1 = sk1["sketch"]
        client.send_command("sketch_populate", sketch=sketch1,
                            elements=hex_elements, constraints=[])
        client.send_command("close_sketch", sketch=sketch1)
        client.send_command("pad", sketch=sketch1, length=spec.height,
                            verify=False, **({"doc": doc} if doc else {}))

        # 3. Center through-hole (pocket through all)
        hole_elements = [{"type": "circle", "cx": 0, "cy": 0,
                          "r": spec.through_hole / 2}]
        sk2 = client.send_command("new_sketch", body=actual_body,
                                  plane="XY", **({"doc": doc} if doc else {}))
        sketch2 = sk2["sketch"]
        client.send_command("sketch_populate", sketch=sketch2,
                            elements=hole_elements, constraints=[])
        client.send_command("close_sketch", sketch=sketch2)

        is_last = placement_kw is None
        pocket_result = client.send_command(
            "pocket", sketch=sketch2, length=0, pocket_type="Through",
            verify=verify and is_last, **({"doc": doc} if doc else {}),
        )

        # 4. Position/rotate if needed
        if placement_kw is not None:
            placement_kw["object_name"] = actual_body
            client.send_command("set_placement", **placement_kw)

        result: dict[str, Any] = {
            "ok": True,
            "body": actual_body,
            "size": size,
            "nut_type": nut_type,
            "across_flats_mm": spec.across_flats,
            "across_corners_mm": spec.across_corners,
            "height_mm": spec.height,
            "thread_diameter_mm": spec.thread_diameter,
        }
        if position is not None:
            result["position"] = position
        if rotation_axis is not None:
            result["rotation_axis"] = rotation_axis
            result["rotation_angle_deg"] = rotation_angle_deg
        # Forward verification images from last pocket
        for key in ("verification_images", "verification_views",
                     "face_map", "operation_summary"):
            if key in pocket_result:
                result[key] = pocket_result[key]
        return result

    except FreeCADConnectionError as e:
        log.error("FAIL cad_nut CONNECTION_ERROR: %s", e)
        return _error_result("CONNECTION_ERROR", str(e))
    except FreeCADCommandError as e:
        log.error("FAIL cad_nut COMMAND_ERROR: %s", e)
        return _error_result("COMMAND_ERROR", str(e))


def cad_find_holes(
    body: str | None = None,
    doc: str | None = None,
    min_diameter: float = 0.0,
    max_diameter: float = 200.0,
) -> dict[str, Any]:
    """Find cylindrical holes in a body and suggest bolt sizes.

    Calls the FreeCAD addon to identify all cylindrical faces, then matches
    each hole diameter against ISO 273 clearance hole tables to suggest the
    appropriate bolt size and fit type.

    Parameters
    ----------
    body : str | None
        Body name to inspect. Uses active body if None.
    doc : str | None
        Document name. Uses active document if None.
    min_diameter : float
        Minimum hole diameter to report (mm).
    max_diameter : float
        Maximum hole diameter to report (mm).
    """
    client = get_client()

    try:
        kw: dict[str, Any] = {
            "min_diameter": min_diameter,
            "max_diameter": max_diameter,
        }
        if body is not None:
            kw["body"] = body
        if doc is not None:
            kw["doc"] = doc

        result = client.send_command("find_holes", **kw)

        # Enrich each hole with bolt size suggestion
        holes = result.get("holes", [])
        for hole in holes:
            match = match_bolt_size(hole["diameter_mm"])
            if match is not None:
                hole["suggested_bolt"] = match

        return {"ok": True, **result}

    except FreeCADConnectionError as e:
        log.error("FAIL cad_find_holes CONNECTION_ERROR: %s", e)
        return _error_result("CONNECTION_ERROR", str(e))
    except FreeCADCommandError as e:
        log.error("FAIL cad_find_holes COMMAND_ERROR: %s", e)
        return _error_result("COMMAND_ERROR", str(e))
