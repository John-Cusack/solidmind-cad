# Motion Validation Policy

Motion validation is **user-initiated and human-gated**. Applies only to mechanisms with moving parts (gears, linkages, cams, belt drives). Skip for static parts.

## Tiers

1. **Tier 1 (Analytical)**: motion.define_mechanism → motion.validate → motion.propagate_motion
2. **Tier 1.5 (Connectivity)**: motion.check_joint_connectivity (pre-export)
3. **Tier 2 (Kinematic)**: motion.create_assembly → motion.drive_joint → motion.check_interference
4. **Tier 3 (Dynamic)**: motion.simulate with backend selection
5. **Tier 3.5 (FEA from Dynamics)**: analysis.stress_from_simulation
   - Extract peak joint forces from motion.simulate results
   - Map to face-based BCs on critical parts
   - Run stress check with safety factor

## Backend Selection

| Robot Type | Backend | Why |
|-----------|---------|-----|
| Drone / multirotor | `gazebo` | PX4 ecosystem, 5-DOF teleop |
| Wheeled vehicle | `gazebo` | ROS ecosystem, lateral velocity |
| Legged robot / hexapod | `isaac` | GPU contact, tripod controller |
| Articulated arm | `isaac` | GPU physics, joint-level control |
| Gear train / linkage | `chrono` | Analytical MBS, batch validation |
| CPU-only | `gazebo` | No GPU required |

## Rules

- **Never auto-run.** Always suggest: "Would you like me to validate the gear ratios?"
- Wait for user approval before escalating tiers.
- Report Tier 1 results before offering Tier 2/3.
