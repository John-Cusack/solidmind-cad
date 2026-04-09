# Design Pipeline (Phased Approach)

The `design.*` tools support a phased design process: understand intent, size the system, define the layout, then build parts. Each phase has a user gate.

## When to Use

MANDATORY for:
- Multi-body assemblies (3+ bodies)
- Mechanisms with moving parts (gears, linkages, robots)
- Designs involving purchased components
- Multiple parts that must interface with each other

Skip ONLY for:
- Single-body parts with all dimensions given
- Quick modifications to existing models
- User explicitly says "just build it"

## Phase 1: Intent

Clarify what's being built: what, what for, hard constraints. No tools needed.

```
design.save_brief(name="Watch Movement", parameters={
    "intent": "mechanical watch going train with motion works",
    "constraints": {"layers": 5, "gear_module": 0.3}
}, status="intent")
```

Gate: present understanding → user confirms.

## Phase 2: Sizing

Engineering calculations and component selection. Pattern: requirements → candidates → check numbers → iterate.

Register each component:
```
design.add_part(brief_id, name="center_wheel", kind="custom", quantity=1,
    specs={"teeth": 80, "module": 0.3, "pitch_diameter": 24, "z_layer": 2})
design.add_part(brief_id, name="center_pinion", kind="custom", quantity=1,
    specs={"teeth": 10, "module": 0.3, "pitch_diameter": 3, "z_layer": 1,
           "same_arbor_as": "center_wheel"})
```

Gate: present component table → user confirms.

## Phase 3: Layout

Define spatial relationships. Dimensions derived from sizing, not guessed.

Define interfaces:
```
design.add_interface(brief_id,
    part_a="barrel", port_a="teeth",
    part_b="center_pinion", port_b="teeth",
    spec={"type": "gear_mesh", "z_layer": 1, "center_distance_mm": 15.5})
```

Add layout positions:
```
design.update_brief(brief_id, parameters={
    "layout": {
        "positions": {"barrel": [-10.6, 10.6], "center_wheel": [0, 0], ...},
        "z_layers": {"layer_1": -2.5, "layer_2": 0, "layer_3": 2.5, ...},
        "arbor_paths": [
            {"from": "fourth_wheel", "to": "second_hand", "xy": [0, -21.5],
             "z_range": [-11.25, 7.5], "clearance_check": ["center_wheel"]}
        ]
    }
})
```

IMPORTANT: Validate the layout BEFORE building:
- Check every arbor path against all gear envelopes (does it intersect?)
- Verify every mesh pair is co-planar in Z
- Confirm center distances match pitch radii sums

Gate: present layout → user approves.

## Phase 4: Build

For each part:
1. `design.get_part(brief_id, "center_wheel")` — pull specs + interfaces
2. `cad.new_body(label="center_wheel")` — create body
3. Build geometry from interface specs (not guessed dimensions)
4. Verify with screenshots
5. `design.update_part(brief_id, "center_wheel", body_label="...", status="built")`

Build order follows dependencies.

After all parts: `design.verify_build(brief_id)` → `design.update_brief(brief_id, status="done")`

## Phase Lifecycle

```
intent → sizing → layout → approved → building → verify → done
         ↑ user    ↑ user    ↑ user              ↑ auto
```

## Articulated Mechanisms

For robots/hexapods/arms: decompose by kinematic segment (rigid portion between joints), not by component type. One body per segment. Purchased servos become pockets in the segment body.
