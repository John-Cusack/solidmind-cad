# FreeCAD → Isaac Sim Pipeline

How a FreeCAD model becomes a walking robot in Isaac Sim — every stage, every
transform, every coordinate frame.

## 1. Overview

```
FreeCAD GUI                MCP Bridge Server              Isaac Bridge
┌─────────────┐            ┌──────────────────┐           ┌───────────────┐
│ PartDesign   │            │                  │           │               │
│ Bodies with  │──export──▶│ export_sim_package│           │               │
│ Placement    │  STL+     │   (tools_cad.py)  │           │               │
│              │  manifest │                  │           │               │
└─────────────┘            │        │         │           │               │
                           │        ▼         │           │               │
                           │ motion.define_   │           │               │
                           │ mechanism        │           │               │
                           │ (motion_models)  │           │               │
                           │        │         │           │               │
                           │        ▼         │           │               │
                           │ build_sim_model  │           │               │
                           │  (sim_export.py) │           │               │
                           │   │ BFS world pos│           │               │
                           │   │ STL → local  │           │               │
                           │   │ joint origins │           │               │
                           │        │         │           │               │
                           │        ▼         │           │               │
                           │  write_urdf      │──URDF──▶ │ import_urdf   │
                           │  (sim_export.py) │  + STLs   │ (runtime_     │
                           │                  │           │  isaac.py)    │
                           └──────────────────┘           │       │       │
                                                          │       ▼       │
                                                          │  USD scene    │
                                                          │  + physics    │
                                                          │  + drives     │
                                                          └───────────────┘
```

The pipeline has five stages:

| # | Stage | Input | Output | Key File |
|---|-------|-------|--------|----------|
| 1 | STL Export | FreeCAD Bodies | STL meshes + manifest | `freecad_addon/commands.py` |
| 2 | Mechanism Definition | User-specified kinematic graph | `Mechanism` object | `server/motion_models.py` |
| 3 | Sim Model Building | Mechanism + manifest | `SimModel` (links + joints) | `server/sim_export.py` |
| 4 | URDF Serialization | `SimModel` | `.urdf` file | `server/sim_export.py` |
| 5 | Isaac Import | URDF + STLs | USD physics scene | `isaac_bridge/runtime_isaac.py` |

---

## 2. Stage 1: FreeCAD STL Export

**Function:** `freecad_addon/commands.py::export_sim_package()`

**What it does:**
- Iterates over all `PartDesign::Body` objects (or a specified subset)
- Exports each Body's `Shape` to an individual STL file
- Collects placement and geometry metadata into a manifest

**Coordinate frame:** STL vertices are in **FreeCAD world coordinates**. The
export calls `shape.exportStl()` which includes the cumulative effect of the
Body's `Placement` *and* any geometry built at world coordinates in sketches.

**Manifest entry per body:**
```python
{
    "name": "Body_Coxa_LF",           # FreeCAD object name
    "label": "Body_Coxa_LF",          # FreeCAD display label
    "mesh_path": "/tmp/sim_pkg_xxx/Body_Coxa_LF.stl",
    "placement": {
        "position": [80.0, 55.0, 25.0],          # mm, world frame
        "rotation_quat": [1.0, 0.0, 0.0, 0.0],   # w,x,y,z
    },
    "bbox_mm": [30.0, 12.0, 12.0],     # bounding box dimensions
    "bbox_min_mm": [65.0, 49.0, 19.0], # bounding box minimum corner
    "volume_mm3": 3240.0,              # shape volume
}
```

**Quaternion convention:** FreeCAD internally stores quaternions as `(x,y,z,w)`
via `Placement.Rotation.Q`. The manifest converts to `(w,x,y,z)` order, which
is what `sim_export.py` expects.

**Key detail:** The STL contains geometry in world coordinates regardless of
whether the Body used `Placement` to position itself or was sketched directly at
world coordinates. This is deliberate — Stage 3 transforms the vertices to
link-local.

---

## 3. Stage 2: Mechanism Definition

**Function:** `server/tools_motion.py` → `motion.define_mechanism` tool
**Model:** `server/motion_models.py`

The mechanism is a directed graph of parts (nodes) and joints (edges).

### PartNode

```python
@dataclass(frozen=True, slots=True)
class PartNode:
    id: str                           # Unique ID, used as URDF link name
    body_name: str | None = None      # FreeCAD Body name (for manifest lookup)
    mesh_path: str | None = None      # Direct STL path (alternative to manifest)
    mass_kg: float | None = None      # Override mass (auto-computed if None)
    inertia_kg_m2: float | None = None  # Scalar → diagonal tensor (Ixx=Iyy=Izz)
    is_ground: bool = False           # True = fixed to world (chassis)
```

### JointEdge

```python
@dataclass(frozen=True, slots=True)
class JointEdge:
    id: str                           # Joint name in URDF
    joint_type: JointType             # revolute, prismatic, fixed, ...
    parent_part: str                  # Parent PartNode.id
    child_part: str                   # Child PartNode.id
    axis: tuple[float, float, float]  # Joint axis (child-local frame)
    origin: tuple[float, float, float] = (0.0, 0.0, 0.0)  # Joint origin mm (world)
    gear_ratio: float | None          # For gear_mesh joints
    teeth_parent: int | None          # Gear teeth (parent)
    teeth_child: int | None           # Gear teeth (child)
    min_angle_deg: float | None       # Revolute joint limits
    max_angle_deg: float | None
    min_travel_mm: float | None       # Prismatic joint limits
    max_travel_mm: float | None
    effort_nm: float | None           # Max torque
    velocity_rad_s: float | None      # Max velocity
    damping: float | None
    friction: float | None
    # Also: mesh_efficiency, internal, link_length_mm (omitted for brevity)
```

### Critical mapping

- `PartNode.id` → URDF `<link name="...">`
- `PartNode.body_name` → used to find the matching manifest entry from Stage 1
- `JointEdge.origin` → **world-frame mm coordinates** of where the joint sits.
  If all zeros, Stage 3 falls back to the child body's manifest placement.
- `JointEdge.axis` → specified in **child link's local frame** (not world frame).
  For Z-axis joints this is `(0,0,1)` everywhere because Z-rotation is invariant
  under Z-rotation. For pitch joints it's `(0,1,0)` — local Y.

### Common pitfall

The mechanism definition is independent of FreeCAD geometry. If joint origins
don't match where the Bodies are actually placed in FreeCAD, the URDF will have
misaligned joints. The manifest fallback (zero-origin → use child placement)
mitigates this but only works when origins are left at `(0,0,0)`.

---

## 4. Stage 3: Sim Model Building

**Function:** `server/sim_export.py::build_sim_model()`

This is the most complex stage. It transforms the mechanism graph + mesh
manifest into a `SimModel` ready for URDF serialization. Five sub-steps:

### 4a. Create SimLinks from parts + manifest

For each `PartNode`, find the matching manifest entry (by `body_name`, then by
`id`). Extract:
- `mesh_path` — STL file location
- `position` — world-frame mm from manifest placement
- `rotation_quat` — orientation from manifest
- Mass — from `PartNode.mass_kg`, or auto-computed from `volume_mm3 × density`
  (default PLA density: 1250 kg/m³)
- Inertia — from `PartNode.inertia_kg_m2`, or auto-computed as box inertia from
  `bbox_mm` dimensions

### 4b. BFS to compute world positions + auto-yaw

Starting from ground parts (chassis), BFS traverses the joint graph to compute
each part's world-frame position and cumulative yaw.

```
For each joint where parent is already resolved:
    1. Get joint origin (mm, world frame)
       - Use JointEdge.origin if non-zero
       - Fall back to child body's manifest placement position
    2. Set child's world position = joint origin
    3. Compute yaw:
       - If parent is a ground part:
         yaw = atan2(dy, dx)  where (dx, dy) = joint_pos - parent_pos
         This orients the child's local +X axis radially outward
       - Otherwise:
         yaw = parent's yaw (inherited, no additional rotation)
```

**Why auto-yaw?** Walking robots have legs arranged radially around a chassis.
Each leg's mesh extends along its local +X axis. The auto-yaw rotates each leg's
frame so +X points outward from the chassis center, aligning the mesh with the
physical leg direction.

### 4c. Compute parent-relative joint origins

URDF requires joint origins relative to the parent link, not in world frame. The
transform is:

```
world_offset = child_world_pos - parent_world_pos      # mm, world frame
local_offset = Rz(-parent_yaw) × world_offset          # rotate into parent local
origin_xyz = local_offset / 1000.0                      # mm → meters
```

Joint RPY (roll-pitch-yaw):
- If the joint adds a yaw (root-attached joints): `rpy = (0, 0, added_yaw)`
- Otherwise: `rpy = (0, 0, 0)` — pitch/roll come from runtime joint angles

### 4d. Transform STL vertices to link-local

**Function:** `_transform_stl_to_link_local(stl_path, world_pos_mm, world_yaw_rad)`

Each STL was exported in world coordinates (Stage 1). Now we transform every
vertex into the link's local frame:

```
For each vertex (x, y, z) in the STL:
    1. Translate: (tx, ty, tz) = (x - world_x, y - world_y, z - world_z)
    2. Rotate by -yaw about Z:
       lx = tx × cos(-yaw) - ty × sin(-yaw)
       ly = tx × sin(-yaw) + ty × cos(-yaw)
       lz = tz
```

Normals are rotated but not translated (they're direction vectors).

**Important limitations:**
- **Yaw-only rotation.** The transform only handles Z-axis rotation (yaw). If a
  body has pitch or roll in its FreeCAD Placement, this won't fully transform to
  link-local coordinates.
- **ASCII STL only.** The transform uses regex to find `vertex` and
  `facet normal` lines. The file is opened in text mode, so binary STL raises
  `UnicodeDecodeError` (caught by `build_sim_model()`, mesh left untransformed).
  FreeCAD's `exportStl()` produces ASCII by default.
- **In-place modification.** The STL file is read and rewritten. The transform
  is idempotent only if world_pos and yaw are both zero (no-op).

### 4e. Ground clearance (optional)

If `ground_clearance_m` is specified:
1. Create an empty `base_link` (no mesh) as the new root
2. Demote the original root link (`is_root=False`)
3. Insert a fixed joint: `base_link → original_root` with
   `origin_xyz = (0, 0, ground_clearance_m)`

This raises the entire robot above the ground plane so legs have room to swing.

---

## 5. Stage 4: URDF Serialization

**Function:** `server/sim_export.py::write_urdf()`

Converts a `SimModel` into URDF XML. Key details:

### Links

```xml
<link name="Body_Coxa_LF">
  <visual>
    <geometry>
      <mesh filename="Body_Coxa_LF.stl" scale="0.001 0.001 0.001"/>
    </geometry>
    <!-- NO <origin> element — mesh is already in link-local coords -->
  </visual>
  <collision>
    <geometry>
      <mesh filename="Body_Coxa_LF.stl" scale="0.001 0.001 0.001"/>
    </geometry>
  </collision>
  <inertial>
    <mass value="0.015"/>
    <inertia ixx="1.2e-06" ixy="0" ixz="0" iyy="3.4e-06" iyz="0" izz="3.8e-06"/>
  </inertial>
</link>
```

- No `<inertial><origin>` — center of mass is assumed at the link frame origin.
  For asymmetric parts this is approximate; see Known Issues.
- `scale="0.001 0.001 0.001"` converts STL vertices from mm to meters (URDF
  standard unit)
- No `<visual><origin>` — the mesh was pre-transformed to link-local in Stage 4d
- Mesh filename is relative to `base_dir` (the URDF directory)

### Joints

```xml
<joint name="coxa_lf" type="revolute">
  <parent link="chassis"/>
  <child link="Body_Coxa_LF"/>
  <origin xyz="0.080 0.055 0.000" rpy="0 0 0.602"/>
  <axis xyz="0 0 1"/>
  <limit lower="-1.0472" upper="1.0472" effort="1.5" velocity="6.28"/>
  <dynamics damping="0.1" friction="0"/>
</joint>
```

- `origin xyz` — parent-relative, in meters (computed in Stage 4c)
- `origin rpy` — contains the auto-yaw for root-attached joints
- `axis xyz` — in child-local frame (passed through from mechanism)
- Default limits: ±60° (≈ ±1.047 rad) when mechanism doesn't specify

---

## 6. Stage 5: Isaac Sim Import

**Function:** `isaac_bridge/runtime_isaac.py::_IsaacWorldEngine.import_urdf()`

### Process

1. Create an `ImportConfig` via `URDFCreateImportConfig` Kit command
2. Configure import settings from `URDFImportConfig` dataclass:
   - `merge_fixed_joints` — if True, fixed joints collapse parent+child into one
     link (reduces DOFs but loses separate link identity)
   - `fix_base` — if True, root link is welded to world
   - `distance_scale` — multiplier on all distances (default 1.0)
   - `default_drive_type` — "position" or "velocity"
   - `default_drive_stiffness` / `default_drive_damping` — PD gains
3. Remove any stale USD prim at the expected path (re-import safety)
4. Execute `URDFParseAndImportFile` — Isaac's URDF importer
5. Post-import: walk all joint prims and force-set drive stiffness/damping
   (`_configure_drives_post_import`) because the importer may override config
   values with its own defaults

### What Isaac does with the URDF

- Resolves mesh paths relative to the URDF file's directory
- Applies the `scale="0.001 0.001 0.001"` from `<mesh>` elements (mm → m)
- Creates USD `Xform` prims for each link
- Creates typed physics joint prims (`PhysicsRevoluteJoint`, etc.)
- Attaches `DriveAPI` to articulated joints with stiffness/damping
- Builds an articulation root for physics simulation

### URDFImportConfig defaults

| Field | Default | Mobile Robot Override |
|-------|---------|---------------------|
| `merge_fixed_joints` | `False` | `True` |
| `fix_base` | `True` | `False` |
| `distance_scale` | `1.0` | — |
| `default_drive_type` | `"position"` | — |
| `default_drive_stiffness` | `1000.0` | `10.0` |
| `default_drive_damping` | `100.0` | `1.0` |

When `robot_type="mobile"` (hexapods, wheeled bots), lower stiffness/damping
values are applied automatically for stable ground interaction.

---

## 7. Coordinate Frame Summary

| Stage | Frame | Units | Key Transform |
|-------|-------|-------|---------------|
| 1. STL Export | FreeCAD world | mm | `shape.exportStl()` includes Body Placement |
| 2. Mechanism | World frame | mm (origin), local (axis) | User-specified, may fall back to manifest |
| 3a. SimLink creation | World frame | mm | Positions copied from manifest |
| 3b. BFS world positions | World frame | mm | `atan2(dy,dx)` for auto-yaw |
| 3c. Joint origins | Parent-local | meters | `Rz(-parent_yaw) × world_offset / 1000` |
| 3d. STL transform | Link-local | mm | Translate + `Rz(-yaw)` per vertex |
| 4. URDF mesh | Link-local | mm (file), m (via scale) | `scale="0.001 0.001 0.001"` |
| 4. URDF joint | Parent-relative | meters + radians | `<origin xyz="..." rpy="...">` |
| 5. Isaac USD | World | meters | Isaac resolves URDF kinematic chain |

### Unit conversions in the pipeline

- FreeCAD → URDF joint origins: `÷ 1000` (mm → m) in `build_sim_model()`
- STL mesh → URDF visual/collision: `scale="0.001"` (mm → m) in `write_urdf()`
- These are **two independent conversions** that must both be correct

---

## 8. Generated vs Hand-Tuned URDF

The pipeline's generated URDF differs from hand-tuned URDFs (like
`hexapod_sim_pkg/Hexapod_v2_1DOF.urdf`) in several structural ways:

### Mesh positioning strategy

| Aspect | Generated URDF | Hand-Tuned URDF |
|--------|---------------|-----------------|
| Mesh coordinates | Link-local (pre-transformed STL) | World or arbitrary origin |
| `<visual><origin>` | **None** — mesh is already local | Used to offset mesh into position |
| Mesh reusability | Each link has unique STL | Could share meshes with different origins |

### Joint tree structure

| Aspect | Generated URDF | Hand-Tuned URDF |
|--------|---------------|-----------------|
| Servo→chassis | Coxa joints directly on chassis with auto-yaw in RPY | Often: servo body as fixed joint at (0,0,0), then hip revolute at world offset |
| Leg orientation | Auto-computed yaw via `atan2(dy,dx)` baked into joint RPY | Manual RPY values, possibly with `<visual><origin>` rotation |
| Intermediate links | One link per mechanism part | May have extra servo/bracket links |

### Practical implications

**Generated approach (pre-transformed STL, no visual origin):**
- Simpler URDF — fewer elements to debug
- STL files are one-use (specific to this export)
- If the STL transform fails (binary STL, non-yaw rotation), meshes appear at
  wrong positions

**Hand-tuned approach (visual origin offsets):**
- STL files can be generic (e.g., exported from CAD at default position)
- `<visual><origin>` explicitly documents the mesh offset
- More verbose but each piece is independently verifiable

---

## 9. Known Issues and Gotchas

### STL transform is yaw-only

`_transform_stl_to_link_local()` applies `Rz(-yaw)` rotation and translation.
If a body has pitch or roll in its FreeCAD world orientation (e.g., a leg segment
angled downward), the transform will not fully convert to link-local coordinates.
The mesh will appear tilted in simulation.

**Workaround:** Ensure FreeCAD bodies that need non-yaw orientations use
`Body.Placement` with only Z-rotation, or handle pitch/roll through joint angles
at runtime.

### Inertial center of mass at link frame origin

`write_urdf()` omits `<origin>` inside `<inertial>`, so URDF parsers assume
the center of mass is at the link frame origin `(0, 0, 0)`. For symmetric
parts centered on the frame origin this is correct, but for asymmetric parts
(e.g., an L-shaped bracket or a leg segment with the joint at one end) the
true CoM is offset. This produces subtly wrong torque/momentum calculations
in physics simulation. A future improvement could compute the CoM from the
mesh bounding box or from FreeCAD's `Shape.CenterOfGravity`.

### ASCII STL required

The regex-based STL transform (`_VERTEX_RE`, `_NORMAL_RE`) only works on ASCII
STL files. `_transform_stl_to_link_local` opens the file in text mode
(`open(path, "r")`), so a binary STL will raise `UnicodeDecodeError` on read.
The caller in `build_sim_model()` catches this exception and logs a warning —
the pipeline continues but the mesh is left in world coordinates (untransformed),
causing it to appear at the wrong position in simulation.

FreeCAD's `shape.exportStl()` produces ASCII STL by default, so this normally
works. But if STL files are post-processed or come from external sources, they
may be binary.

### Manifest fallback for zero-origin joints

When `JointEdge.origin` is `(0, 0, 0)` (unset), `build_sim_model()` falls back
to the child body's manifest placement position. This is convenient for simple
cases but can mask errors — if you intended the joint to be at the world origin,
it will silently get moved to the body's FreeCAD position instead.

### `merge_fixed_joints` collapses links

Isaac Sim's `merge_fixed_joints=True` (the default for mobile robots) merges
fixed-joint-connected links into a single physics body. This means:

- Servo bodies connected to the chassis via fixed joints disappear as separate
  entities
- The merged body's collision shape combines both meshes
- Joint names and link names in the USD may not match the URDF 1:1
- DOF mapping (`dof_index_map`) works on the post-merge joint names

If you have servo → chassis (fixed) → coxa (revolute) and merge is on, the servo
and chassis become one link, and the coxa joint connects directly to the merged
body.

### Quaternion convention mismatch

FreeCAD's `Placement.Rotation.Q` returns `(x, y, z, w)`. The manifest and
`SimLink` use `(w, x, y, z)`. The conversion happens in
`export_sim_package()`:

```python
quat = plc.Rotation.Q  # (x, y, z, w) in FreeCAD
"rotation_quat": [quat[3], quat[0], quat[1], quat[2]],  # w,x,y,z
```

If new code touches quaternions, verify the convention at each boundary.

### URDF validation

`sim_export.py` includes two validators that run post-generation:

- `validate_urdf()` — structural checks (limits, axis magnitude, connectivity,
  mesh scale consistency, duplicate meshes)
- `validate_urdf_fk()` — forward-kinematics checks (chassis height, link chain
  gaps, tibia tip height, coxa yaw, bilateral symmetry, mesh overlap)

These are called by `cad_export_sim_package()` in `tools_cad.py` and findings
are returned alongside the URDF path. Always check the findings — `BLOCK`
severity means the URDF is likely unusable.
