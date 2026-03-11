# Tool Design: `cad.create_primitive`

## Problem

Creating a simple positioned solid (box, cylinder) currently requires 4 sequential MCP tool calls:

1. `cad.new_body` — create a PartDesign Body
2. `cad.sketch` — create + populate + close a sketch with a rect/circle
3. `cad.pad` — extrude the sketch
4. `cad.set_placement` — position and orient the body

This is fine for one-off parts, but for tasks that require many identical or similar bodies (e.g., 18 servo motors on a hexapod, fasteners at bolt holes, standoffs on a PCB), the 4× round-trip overhead makes the workflow slow and verbose.

## Solution

A single compound MCP tool `cad.create_primitive` that creates a positioned solid body in one call. Internally it chains the same 4 addon commands, but the LLM only issues one tool call.

Optional: a batch variant that accepts multiple primitives in a single call for maximum throughput.

## API

### MCP Tool Schema

```json
{
    "name": "cad.create_primitive",
    "description": "Create a simple solid body (box or cylinder) with position and orientation in one call. Combines body creation, sketch, pad, and placement. Use for fasteners, motor housings, standoffs, and any case where you need a positioned primitive without complex features.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Body name (e.g. 'Servo_hip_yaw_L1')"
            },
            "shape": {
                "type": "string",
                "enum": ["box", "cylinder"],
                "description": "Primitive shape type"
            },
            "dimensions": {
                "type": "object",
                "description": "Shape dimensions in mm. Box: {length, width, height}. Cylinder: {radius, height}.",
                "properties": {
                    "length": {"type": "number", "description": "Box length along local X (mm)"},
                    "width": {"type": "number", "description": "Box width along local Y (mm)"},
                    "height": {"type": "number", "description": "Extrusion height along local Z (mm)"},
                    "radius": {"type": "number", "description": "Cylinder radius (mm)"}
                }
            },
            "position": {
                "type": "array",
                "items": {"type": "number"},
                "description": "World position [x, y, z] in mm. The primitive is centered at this point."
            },
            "rotation_axis": {
                "type": "array",
                "items": {"type": "number"},
                "description": "Rotation axis [ax, ay, az] (default: [0, 0, 1])"
            },
            "rotation_angle_deg": {
                "type": "number",
                "description": "Rotation angle in degrees (default: 0)",
                "default": 0
            },
            "verify": {
                "type": "boolean",
                "description": "Capture verification screenshots (default: false for primitives)",
                "default": false
            },
            "doc": {
                "type": "string",
                "description": "Document name (optional)"
            }
        },
        "required": ["name", "shape", "dimensions"],
        "additionalProperties": false
    }
}
```

### Batch Variant

```json
{
    "name": "cad.create_primitives",
    "description": "Create multiple simple solid bodies in one call. Each item has the same schema as cad.create_primitive. Use when placing many identical or similar bodies (servos, fasteners, standoffs).",
    "inputSchema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "description": "List of primitive specs",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "shape": {"type": "string", "enum": ["box", "cylinder"]},
                        "dimensions": {"type": "object"},
                        "position": {"type": "array", "items": {"type": "number"}},
                        "rotation_axis": {"type": "array", "items": {"type": "number"}},
                        "rotation_angle_deg": {"type": "number", "default": 0}
                    },
                    "required": ["name", "shape", "dimensions"]
                }
            },
            "verify": {
                "type": "boolean",
                "description": "Capture one verification screenshot after all primitives are created (default: true)",
                "default": true
            },
            "doc": {"type": "string", "description": "Document name (optional)"}
        },
        "required": ["items"],
        "additionalProperties": false
    }
}
```

## Architecture

The tool spans three layers. Here is exactly what changes in each:

### Layer 1: FreeCAD Addon Command (`freecad_addon/commands.py`)

Add a new command handler `create_primitive` that runs entirely inside FreeCAD's Python environment. This is where the actual FreeCAD API calls happen.

```python
def create_primitive(
    name: str,
    shape: str,
    dimensions: dict[str, float],
    position: list[float] | None = None,
    rotation_axis: list[float] | None = None,
    rotation_angle_deg: float = 0.0,
    verify: bool = False,
    doc: str | None = None,
) -> dict[str, Any]:
```

**Implementation steps inside FreeCAD:**

1. Get the document (`_get_doc(doc)`)
2. Create a PartDesign Body: `d.addObject("PartDesign::Body", name)`
3. Create a sketch on XY plane, attached to the body
4. Add geometry to the sketch:
   - `box`: a centered rectangle `{"type": "rect", "x": -length/2, "y": -width/2, "w": length, "h": width}`
   - `cylinder`: a centered circle `{"type": "circle", "cx": 0, "cy": 0, "r": radius}`
5. Close the sketch
6. Pad the sketch with `symmetric=True` so the solid is centered on the sketch plane (centered on all 3 axes)
   - Pad length = `dimensions["height"]`
7. Set placement on the Body object using position + rotation_axis + rotation_angle_deg
8. Single `d.recompute()` at the end
9. If `verify=True`, capture verification screenshots
10. Return result dict

**Return value:**

```json
{
    "body": "Servo_hip_yaw_L1",
    "pad": "Pad",
    "sketch": "Sketch",
    "position": [70.0, 75.0, 158.0],
    "rotation_angle_deg": 47.0,
    "rotation_axis": [0.0, 0.0, 1.0],
    "bbox_mm": [32.0, 24.0, 24.0]
}
```

**Register the handler** at the bottom of `commands.py` in the `COMMAND_HANDLERS` dict:

```python
COMMAND_HANDLERS: dict[str, Any] = {
    ...
    "create_primitive": create_primitive,
    ...
}
```

**Batch handler** `create_primitives` (plural) loops over items, calling the same internal logic, with a single final recompute:

```python
def create_primitives(
    items: list[dict[str, Any]],
    verify: bool = True,
    doc: str | None = None,
) -> dict[str, Any]:
```

Returns `{"created": [...], "failed": [...], "verification_images": [...]}`.

### Layer 2: MCP Server Bridge (`server/tools_cad.py`)

Add two new functions that translate MCP tool args into addon socket commands:

```python
@_wrap
def cad_create_primitive(
    name: str,
    shape: str,
    dimensions: dict[str, Any],
    position: list[float] | None = None,
    rotation_axis: list[float] | None = None,
    rotation_angle_deg: float = 0.0,
    verify: bool = False,
    doc: str | None = None,
) -> dict[str, Any]:
    """Create a simple positioned solid body in one call."""
    client = get_client()
    kwargs: dict[str, Any] = {
        "name": name,
        "shape": shape,
        "dimensions": dimensions,
        "verify": verify,
    }
    if position is not None:
        kwargs["position"] = position
    if rotation_axis is not None:
        kwargs["rotation_axis"] = rotation_axis
    kwargs["rotation_angle_deg"] = rotation_angle_deg
    if doc is not None:
        kwargs["doc"] = doc
    result = client.send_command("create_primitive", **kwargs)
    return {"ok": True, **result}


@_wrap
def cad_create_primitives(
    items: list[dict[str, Any]],
    verify: bool = True,
    doc: str | None = None,
) -> dict[str, Any]:
    """Create multiple simple positioned solid bodies in one call."""
    client = get_client()
    kwargs: dict[str, Any] = {"items": items, "verify": verify}
    if doc is not None:
        kwargs["doc"] = doc
    # Generous timeout: 5s per item + 30s base
    timeout = max(30.0, 30.0 + len(items) * 5.0)
    result = client.send_command("create_primitives", timeout=timeout, **kwargs)
    return {"ok": True, **result}
```

### Layer 3: MCP Tool Registration (`server/main.py`)

1. **Import** the new functions at the top alongside existing imports:

```python
from server.tools_cad import (
    ...
    cad_create_primitive,
    cad_create_primitives,
    ...
)
```

2. **Add tool schemas** to the `_TOOL_SCHEMAS` list (the JSON schemas shown in the API section above).

3. **Add dispatch entries** in `_CAD_DISPATCH`:

```python
_CAD_DISPATCH: dict[str, Any] = {
    ...
    "cad.create_primitive": cad_create_primitive,
    "cad.create_primitives": cad_create_primitives,
    ...
}
```

## Geometry Details

### Box

The sketch rectangle is centered at the origin: `x = -length/2, y = -width/2, w = length, h = width`. The pad uses `symmetric=True` with the full height, so the box extends from `-height/2` to `+height/2` in local Z. After placement, the box center is exactly at the specified `position`.

### Cylinder

The sketch circle is centered at the origin with the given radius. The pad uses `symmetric=True` with the full height. After placement, the cylinder center is exactly at the specified `position`.

### Centering Convention

All primitives are centered at `position`. This is the most useful convention for placing components at joint locations — the caller specifies where the center should be, not a corner.

## Validation

The addon command should validate:

- `shape` is one of `"box"`, `"cylinder"`
- `dimensions` contains the required keys for the shape (`length`, `width`, `height` for box; `radius`, `height` for cylinder)
- All dimension values are positive numbers
- `rotation_axis` (if given) is a 3-element list

On validation failure, raise `ValueError` with a clear message (the existing error handling in the socket server will wrap it).

## Testing

### Unit Tests (`tests/test_create_primitive.py`)

Test the MCP bridge functions with a mocked FreeCAD client, following the existing pattern in `tests/test_tools_cad.py`:

1. **`test_box_basic`** — create a box, verify the correct sequence of addon commands is sent: `new_body` → `new_sketch` → `sketch_populate` (rect) → `close_sketch` → `pad` (symmetric) → `set_placement`
2. **`test_cylinder_basic`** — same for cylinder, verify sketch_populate uses a circle element
3. **`test_default_placement`** — omit position/rotation, verify no `set_placement` call or placement at origin
4. **`test_invalid_shape`** — verify error for unsupported shape type
5. **`test_missing_dimensions`** — verify error when required dimension keys are missing
6. **`test_batch_creates_multiple`** — `create_primitives` with 3 items, verify 3 bodies created
7. **`test_batch_partial_failure`** — one item has invalid dimensions, verify it appears in `failed` list and others succeed

## Example Usage

### Single servo motor

```
cad.create_primitive(
    name="Servo_hip_yaw_L1",
    shape="box",
    dimensions={"length": 32, "width": 24, "height": 24},
    position=[70, 75, 158],
    rotation_axis=[0, 0, 1],
    rotation_angle_deg=47
)
```

### 18 servos in one call

```
cad.create_primitives(items=[
    {
        "name": "Servo_hip_yaw_L1",
        "shape": "box",
        "dimensions": {"length": 32, "width": 24, "height": 24},
        "position": [70, 75, 158],
        "rotation_axis": [0, 0, 1],
        "rotation_angle_deg": 47
    },
    {
        "name": "Servo_hip_pitch_L1",
        "shape": "box",
        "dimensions": {"length": 32, "width": 24, "height": 24},
        "position": [105.5, 113, 158],
        "rotation_axis": [0, 0, 1],
        "rotation_angle_deg": 137
    },
    ...
])
```

## Files to Modify

| File | Change |
|------|--------|
| `freecad_addon/commands.py` | Add `create_primitive()` and `create_primitives()` functions; register in `COMMAND_HANDLERS` |
| `server/tools_cad.py` | Add `cad_create_primitive()` and `cad_create_primitives()` bridge functions with `@_wrap` |
| `server/main.py` | Import new functions; add JSON schemas to `_TOOL_SCHEMAS`; add entries to `_CAD_DISPATCH` |
| `tests/test_create_primitive.py` | New test file with unit tests (mocked client) |
| `CLAUDE.md` | Add `create_primitive`, `create_primitives` to the tool table in the `cad.*` group |

## Implementation Notes

- The addon-side `create_primitive` should reuse existing helper functions: `_get_doc()`, `_find_parent_body()`, `_recompute_and_check()`, and the sketch geometry logic from `sketch_populate()`. Do not duplicate that code — call into the existing functions or factor out shared logic.
- The pad should use `verify=False` internally — the compound tool controls its own verification.
- For the batch variant, defer all `recompute()` calls until after all bodies are created, then do a single final recompute for performance. If that causes issues with sketch references, fall back to recomputing per-body.
- The `@_wrap` decorator on the bridge function handles `FreeCADConnectionError` and `FreeCADCommandError` automatically — no need for explicit try/except.
- Follow existing code style: 4-space indent, type hints, `from __future__ import annotations`.
