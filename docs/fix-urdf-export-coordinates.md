# Fix: URDF Export Coordinate System Bugs

## Problem Statement

When exporting a hexapod (or any multi-body mechanism) to URDF for Isaac Sim, the robot explodes on import because mesh vertices are in **world coordinates** but URDF expects **body-local coordinates**. Even after fixing the mesh export, the robot collapses because standing-pose pitch angles are double-counted — baked into both the URDF joint RPY AND the IK controller's target angles.

## Three Bugs

### Bug 1: STL meshes exported in world coordinates

**File:** `freecad_addon/commands.py`, `export_sim_package()` (~line 2125)

**Current behavior:** `body_obj.Shape.exportStl()` exports vertices with the Body's Placement applied (world coordinates). The chassis at z=158mm has STL vertices at z=158-166mm instead of z=0-8mm.

**Expected behavior:** Meshes should be in body-local coordinates (origin at body's local 0,0,0). The URDF joint chain handles world positioning.

**Fix (already applied):** Before export, strip the Placement:
```python
plc = body_obj.Placement
if plc.Base.Length > 1e-6 or plc.Rotation.Angle > 1e-6:
    local_shape = shape.copy()
    local_shape.transformShape(plc.inverse().toMatrix())
else:
    local_shape = shape
local_shape.exportStl(mesh_path)
```

**Comment fix (already applied):** Updated the comment in `server/sim_export.py` line 519 from "FreeCAD's Shape.exportStl() exports vertices in body-local coordinates (pre-Placement)" to the corrected text.

### Bug 2: Joint RPY bakes standing-pose pitch angles

**File:** `server/sim_export.py`, `build_sim_model()` (lines 370-384)

**Current behavior:** For non-root joints (femur, tibia), `origin_rpy` is computed from the relative quaternion between parent and child FreeCAD body placements:

```python
q_parent_inv = _quat_inverse(*q_parent)
q_relative = _quat_multiply(*q_parent_inv, *q_child)
origin_rpy = _quat_to_rpy(*q_relative)
```

This produces `rpy=(~0, 0.5236, ~0)` for femur joints (30° pitch) and `rpy=(~0, 0.6981, ~0)` for tibia joints (40° pitch). These angles represent the FreeCAD model's standing pose.

**Why it breaks:** The Hexapod3DOFController (`isaac_bridge/controllers.py`) uses IK that assumes joint angle 0 = legs straight horizontal. It computes standing-pose angles (~50° femur, ~-130° tibia) as targets. When these targets are applied ON TOP of the baked-in pitch, the total angle is ~80° femur, causing the robot to collapse.

**Standard URDF convention:** Joint angle 0 should represent a neutral/straight configuration. The standing pose should come entirely from joint angle targets set by the controller.

**Fix:** For joints where `added_yaw == 0` (i.e., non-root joints like femur/tibia), set `origin_rpy = (0, 0, 0)` instead of computing from FreeCAD body quaternions. The current fallback code block (lines 374-384) should be replaced:

```python
# BEFORE (wrong):
if abs(added_yaw) > 1e-9:
    origin_rpy = (0.0, 0.0, added_yaw)
else:
    # Fall back to manifest placements (relative quaternion → rpy).
    parent_plc = link_placement.get(parent_link)
    child_plc = link_placement.get(child_link)
    if parent_plc is not None and child_plc is not None:
        q_parent = parent_plc[1]
        q_child = child_plc[1]
        q_parent_inv = _quat_inverse(*q_parent)
        q_relative = _quat_multiply(*q_parent_inv, *q_child)
        origin_rpy = _quat_to_rpy(*q_relative)
    else:
        origin_rpy = (0.0, 0.0, 0.0)

# AFTER (correct):
if abs(added_yaw) > 1e-9:
    origin_rpy = (0.0, 0.0, added_yaw)
else:
    origin_rpy = (0.0, 0.0, 0.0)
```

**Rationale:** With body-local meshes (Bug 1 fix), each link's mesh is axis-aligned at the body origin. The URDF joint chain positions links via `origin_xyz` (translation) and yaw (from `added_yaw` for root-attached joints). Pitch/roll orientation should come from joint angle targets, not baked into the joint RPY.

### Bug 3: Joint axes in world frame instead of local frame

**File:** `server/sim_export.py`, `build_sim_model()` (line 440)

**Current behavior:** The joint axis is passed through directly from the mechanism definition:
```python
axis=jedge.axis,  # e.g., (-0.731055, 0.682318, 0) for femur_lf
```

These axis vectors were defined in the **world frame** (perpendicular to the leg direction in world space). With body-local meshes, the joint axis should be in the **child link's local frame**.

**Expected behavior:** For a hexapod with body-local meshes:
- **Coxa joints** (yaw): axis = `(0, 0, 1)` — rotation about Z ✓ (already correct)
- **Femur joints** (pitch): axis = `(0, 1, 0)` — rotation about local Y (perpendicular to leg in leg-local frame)
- **Tibia joints** (pitch): axis = `(0, 1, 0)` — same as femur

**Fix:** When `build_sim_model` generates joints for body-local-coordinate URDFs, the axis should be transformed from world frame to the child link's local frame. For the common case of revolute joints on legs radiating from a central body:

The simplest approach: rotate the world-frame axis by the **inverse** of the cumulative yaw rotation for that joint's parent chain. Since the only frame rotation in the URDF is the coxa yaw, the femur/tibia axes need to be rotated by `-coxa_yaw` to get into the coxa-local frame.

```python
# For non-root joints, rotate axis into parent's local frame
parent_yaw = part_world_yaw.get(jedge.parent_part, 0.0)
if abs(parent_yaw) > 1e-9:
    ax, ay, az = jedge.axis
    c = math.cos(-parent_yaw)
    s = math.sin(-parent_yaw)
    local_axis = (ax * c - ay * s, ax * s + ay * c, az)
    # Re-normalize
    mag = math.sqrt(sum(a*a for a in local_axis))
    local_axis = tuple(a / mag for a in local_axis)
else:
    local_axis = jedge.axis
```

But the **cleaner approach** is: since the only rotations in the URDF chain are the coxa yaw rotations, and the body-local meshes have consistent axis alignment, femur/tibia pitch axes should ALWAYS be `(0, 1, 0)` in the local frame. The mechanism definition's world-frame axes contain the same information as the coxa yaw + a standard local pitch axis.

**Recommended implementation:** Add a flag or heuristic to detect when body-local meshes are used, and compute axes in the local frame. Or, transform the mechanism axis through the inverse of `part_world_yaw` (which is already tracked in `build_sim_model`).

## Testing

### Unit test for Bug 1
In `tests/test_sim_export.py`, add a test that:
1. Creates a mock body manifest with non-identity placements
2. Verifies that exported STL vertices are in body-local coordinates (bounding box starts near origin)

### Unit test for Bug 2
1. Create a mechanism with a ground part and two child joints (simulating chassis → coxa → femur)
2. Set the manifest placements to have rotated quaternions (simulating standing pose)
3. Call `build_sim_model()`
4. Assert that non-root joint `origin_rpy` is `(0, 0, 0)` (no baked pitch)
5. Assert that root-attached joint `origin_rpy` has the correct yaw only

### Unit test for Bug 3
1. Create a mechanism with world-frame axes like `(-0.731, 0.682, 0)`
2. Call `build_sim_model()` with the coxa yaw computed
3. Assert that the resulting SimJoint axis is in local frame (e.g., `(0, 1, 0)` for pitch)

### Integration test
1. Generate URDF from the hexapod mechanism
2. Parse the URDF XML
3. Verify all femur/tibia joints have `rpy="0 0 0"` and `axis="0 1 0"`
4. Verify coxa joints have `rpy="0 0 <yaw>"` and `axis="0 0 1"`

## Impact

These fixes affect all URDF exports from `cad.export_sim_package` + `build_sim_model`. The current code only produces correct URDFs when all bodies have identity placement (origin at world 0,0,0 with no rotation), which is rare for assembled mechanisms.

## Files Changed

| File | Status | Description |
|------|--------|-------------|
| `freecad_addon/commands.py` | **Done** | Strip Placement before STL export |
| `server/sim_export.py` | **TODO** | Fix joint RPY (Bug 2) and axis (Bug 3); update comment (done) |
| `tests/test_sim_export.py` | **TODO** | Add unit tests for all three fixes |
