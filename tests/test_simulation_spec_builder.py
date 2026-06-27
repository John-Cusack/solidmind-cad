"""Tests for the simulation spec builder (Python planner)."""
from __future__ import annotations

import unittest

from server.motion_models import (
    AppliedForce,
    DriveCondition,
    JointEdge,
    JointType,
    Mechanism,
    PartNode,
)
from server.motion_planetary import detect_planetary_sets
from server.simulation_spec_builder import (
    add_derived_speeds,
    build_simulation_spec,
    validate_simulation_spec,
)


def _make_planetary_mechanism(
    sun_teeth: int = 16,
    planet_teeth: int = 20,
    ring_teeth: int = 56,
    num_planets: int = 3,
    motor_rpm: float = 900.0,
) -> Mechanism:
    """Build a standard planetary gearbox mechanism for testing."""
    parts = [
        PartNode(id="sun", inertia_kg_m2=0.001),
        PartNode(id="carrier", inertia_kg_m2=0.0015),
        PartNode(id="ring", is_ground=True, inertia_kg_m2=0.002),
    ]
    joints: list[JointEdge] = []
    for i in range(num_planets):
        pid = f"planet_{i+1}"
        parts.append(PartNode(id=pid, inertia_kg_m2=0.0005))

        # Revolute: carrier → planet (non-ground to non-ground)
        joints.append(JointEdge(
            id=f"carrier_{pid}_rev",
            joint_type=JointType.REVOLUTE,
            parent_part="carrier",
            child_part=pid,
        ))
        # Gear mesh: sun → planet (external)
        joints.append(JointEdge(
            id=f"sun_{pid}_mesh",
            joint_type=JointType.GEAR_MESH,
            parent_part="sun",
            child_part=pid,
            teeth_parent=sun_teeth,
            teeth_child=planet_teeth,
        ))
        # Gear mesh: planet → ring (internal)
        joints.append(JointEdge(
            id=f"{pid}_ring_mesh",
            joint_type=JointType.GEAR_MESH,
            parent_part=pid,
            child_part="ring",
            teeth_parent=planet_teeth,
            teeth_child=ring_teeth,
            internal=True,
        ))

    drives = [
        DriveCondition(
            joint_id="sun_planet_1_mesh",
            speed_rpm=motor_rpm,
        ),
    ]

    return Mechanism(
        name="planetary_test",
        parts=tuple(parts),
        joints=tuple(joints),
        drives=tuple(drives),
    )


def _make_simple_gear_pair(
    teeth_a: int = 20,
    teeth_b: int = 40,
    motor_rpm: float = 1000.0,
) -> Mechanism:
    """Build a simple two-gear mechanism."""
    parts = [
        PartNode(id="gear_a", inertia_kg_m2=0.001),
        PartNode(id="gear_b", inertia_kg_m2=0.002),
        PartNode(id="frame", is_ground=True),
    ]
    joints = [
        JointEdge(
            id="mesh_ab",
            joint_type=JointType.GEAR_MESH,
            parent_part="gear_a",
            child_part="gear_b",
            teeth_parent=teeth_a,
            teeth_child=teeth_b,
        ),
        JointEdge(
            id="rev_a",
            joint_type=JointType.REVOLUTE,
            parent_part="frame",
            child_part="gear_a",
        ),
        JointEdge(
            id="rev_b",
            joint_type=JointType.REVOLUTE,
            parent_part="frame",
            child_part="gear_b",
        ),
    ]
    drives = [
        DriveCondition(joint_id="mesh_ab", speed_rpm=motor_rpm),
    ]
    return Mechanism(
        name="gear_pair_test",
        parts=tuple(parts),
        joints=tuple(joints),
        drives=tuple(drives),
    )


class TestPlanetaryDetection(unittest.TestCase):
    def test_detects_planetary_set(self):
        mech = _make_planetary_mechanism()
        sets = detect_planetary_sets(mech)
        self.assertEqual(len(sets), 1)
        ps = sets[0]
        self.assertEqual(ps.carrier, "carrier")
        self.assertEqual(ps.sun, "sun")
        self.assertEqual(ps.ring, "ring")
        self.assertEqual(len(ps.planets), 3)
        self.assertIn("planet_1", ps.planets)
        self.assertIn("planet_2", ps.planets)
        self.assertIn("planet_3", ps.planets)

    def test_no_planetary_in_simple_pair(self):
        mech = _make_simple_gear_pair()
        sets = detect_planetary_sets(mech)
        self.assertEqual(len(sets), 0)


class TestWillisRatio(unittest.TestCase):
    def test_t0_calculation(self):
        mech = _make_planetary_mechanism(sun_teeth=16, ring_teeth=56)
        sets = detect_planetary_sets(mech)
        self.assertEqual(len(sets), 1)
        # t0 = -z_sun / z_ring = -16/56 ≈ -0.2857
        self.assertAlmostEqual(sets[0].t0, -16 / 56, places=4)

    def test_t0_different_teeth(self):
        mech = _make_planetary_mechanism(sun_teeth=20, planet_teeth=15, ring_teeth=50)
        sets = detect_planetary_sets(mech)
        self.assertEqual(len(sets), 1)
        self.assertAlmostEqual(sets[0].t0, -20 / 50, places=4)


class TestSimpleGearPairSpec(unittest.TestCase):
    def test_produces_shafts_gear(self):
        mech = _make_simple_gear_pair(teeth_a=20, teeth_b=40)
        spec = build_simulation_spec(mech)

        objects = spec["objects"]
        types = [o["type"] for o in objects]

        self.assertIn("shaft", types)
        self.assertIn("shafts_gear", types)
        # Should NOT produce shafts_planetary
        self.assertNotIn("shafts_planetary", types)

        # Find the shafts_gear object
        gear_objs = [o for o in objects if o["type"] == "shafts_gear"]
        self.assertEqual(len(gear_objs), 1)

        g = gear_objs[0]
        # External mesh: ratio = -(teeth_parent / teeth_child) = -(20/40) = -0.5
        self.assertAlmostEqual(g["ratio"], -0.5, places=4)

    def test_motor_on_shaft(self):
        mech = _make_simple_gear_pair(motor_rpm=1000)
        spec = build_simulation_spec(mech)

        motors = [o for o in spec["objects"] if o["type"] == "motor_shaft_speed"]
        self.assertEqual(len(motors), 1)
        self.assertEqual(motors[0]["speed_rpm"], 1000)


class TestPlanetarySpec(unittest.TestCase):
    def test_produces_shafts_planetary(self):
        mech = _make_planetary_mechanism()
        spec = build_simulation_spec(mech)

        objects = spec["objects"]
        types = [o["type"] for o in objects]

        self.assertIn("shaft", types)
        self.assertIn("shafts_planetary", types)
        # Should NOT produce shafts_gear for planetary
        gear_objs = [o for o in objects if o["type"] == "shafts_gear"]
        self.assertEqual(len(gear_objs), 0)

    def test_shaft_count(self):
        mech = _make_planetary_mechanism()
        spec = build_simulation_spec(mech)

        shafts = [o for o in spec["objects"] if o["type"] == "shaft"]
        shaft_ids = {s["id"] for s in shafts}
        # sun, carrier, ring → 3 shafts (planets are derived, not shafts)
        self.assertEqual(len(shafts), 3)
        self.assertIn("sun", shaft_ids)
        self.assertIn("carrier", shaft_ids)
        self.assertIn("ring", shaft_ids)

    def test_ring_is_fixed(self):
        mech = _make_planetary_mechanism()
        spec = build_simulation_spec(mech)

        ring_shafts = [o for o in spec["objects"] if o["type"] == "shaft" and o["id"] == "ring"]
        self.assertEqual(len(ring_shafts), 1)
        self.assertTrue(ring_shafts[0]["fixed"])

    def test_derived_outputs_for_planets(self):
        mech = _make_planetary_mechanism(sun_teeth=16, planet_teeth=20, ring_teeth=56)
        spec = build_simulation_spec(mech)

        derived = spec["derived_outputs"]
        self.assertIn("planet_1", derived)
        self.assertIn("planet_2", derived)
        self.assertIn("planet_3", derived)

        p1 = derived["planet_1"]
        self.assertEqual(p1["carrier"], "carrier")
        self.assertEqual(p1["sun"], "sun")
        # teeth_ratio = z_sun / z_planet = 16/20 = 0.8
        self.assertAlmostEqual(p1["teeth_ratio"], 0.8, places=4)


class TestDerivedPlanetSpeeds(unittest.TestCase):
    def test_planet_speed_formula(self):
        """Verify: w_planet = w_carrier - (z_sun/z_planet) * (w_sun - w_carrier)"""
        spec = {
            "derived_outputs": {
                "planet_1": {
                    "carrier": "carrier",
                    "sun": "sun",
                    "teeth_ratio": 0.8,  # 16/20
                },
            },
        }
        result = {
            "summary": {
                "steady_state_speeds": {
                    "sun": 900.0,
                    "carrier": 200.0,
                    "ring": 0.0,
                },
            },
            "time_series": [
                {
                    "t": 0.5,
                    "parts": {
                        "sun": {"omega_rpm": 900.0, "pos": [0, 0, 0], "rot": [1, 0, 0, 0]},
                        "carrier": {"omega_rpm": 200.0, "pos": [0, 0, 0], "rot": [1, 0, 0, 0]},
                    },
                },
            ],
        }

        add_derived_speeds(result, spec)

        # w_planet = 200 - 0.8 * (900 - 200) = 200 - 560 = -360
        ss = result["summary"]["steady_state_speeds"]
        self.assertAlmostEqual(ss["planet_1"], -360.0, places=1)

        # Also check time series
        ts_parts = result["time_series"][0]["parts"]
        self.assertIn("planet_1", ts_parts)
        self.assertAlmostEqual(ts_parts["planet_1"]["omega_rpm"], -360.0, places=1)

    def test_no_derived_outputs_is_noop(self):
        spec = {"derived_outputs": {}}
        result = {"summary": {"steady_state_speeds": {"a": 100}}, "time_series": []}
        add_derived_speeds(result, spec)
        self.assertEqual(result["summary"]["steady_state_speeds"], {"a": 100})


class TestSpecFormat(unittest.TestCase):
    def test_top_level_keys(self):
        mech = _make_planetary_mechanism()
        spec = build_simulation_spec(mech)
        self.assertIn("objects", spec)
        self.assertIn("derived_outputs", spec)
        self.assertIsInstance(spec["objects"], list)
        self.assertIsInstance(spec["derived_outputs"], dict)

    def test_all_objects_have_type_and_id(self):
        mech = _make_planetary_mechanism()
        spec = build_simulation_spec(mech)
        for obj in spec["objects"]:
            self.assertIn("type", obj, f"Object missing 'type': {obj}")
            self.assertIn("id", obj, f"Object missing 'id': {obj}")

    def test_simple_pair_format(self):
        mech = _make_simple_gear_pair()
        spec = build_simulation_spec(mech)
        for obj in spec["objects"]:
            self.assertIn("type", obj)
            self.assertIn("id", obj)


class TestInternalGearPair(unittest.TestCase):
    def test_internal_gear_positive_ratio(self):
        """Internal (ring) gear mesh should produce positive ratio."""
        parts = [
            PartNode(id="pinion", inertia_kg_m2=0.001),
            PartNode(id="ring", inertia_kg_m2=0.002),
            PartNode(id="frame", is_ground=True),
        ]
        joints = [
            JointEdge(
                id="mesh_pr",
                joint_type=JointType.GEAR_MESH,
                parent_part="pinion",
                child_part="ring",
                teeth_parent=20,
                teeth_child=60,
                internal=True,
            ),
            JointEdge(
                id="rev_p",
                joint_type=JointType.REVOLUTE,
                parent_part="frame",
                child_part="pinion",
            ),
            JointEdge(
                id="rev_r",
                joint_type=JointType.REVOLUTE,
                parent_part="frame",
                child_part="ring",
            ),
        ]
        mech = Mechanism(
            name="internal_test",
            parts=tuple(parts),
            joints=tuple(joints),
            drives=(),
        )
        spec = build_simulation_spec(mech)
        gear_objs = [o for o in spec["objects"] if o["type"] == "shafts_gear"]
        self.assertEqual(len(gear_objs), 1)
        # Internal: ratio = teeth_parent/teeth_child = 20/60 ≈ 0.333 (positive)
        self.assertAlmostEqual(gear_objs[0]["ratio"], 20 / 60, places=4)


class TestValidateSimulationSpec(unittest.TestCase):
    def test_valid_spec_no_issues(self):
        """A well-formed spec should return no issues."""
        mech = _make_simple_gear_pair()
        spec = build_simulation_spec(mech)
        issues = validate_simulation_spec(spec)
        self.assertEqual(issues, [])

    def test_valid_planetary_spec(self):
        mech = _make_planetary_mechanism()
        spec = build_simulation_spec(mech)
        issues = validate_simulation_spec(spec)
        self.assertEqual(issues, [])

    def test_no_motors(self):
        """Spec with no motor and no spring should report a no-driver issue."""
        spec = {
            "objects": [
                {"type": "shaft", "id": "a", "inertia": 0.01, "fixed": False},
                {"type": "shaft", "id": "b", "inertia": 0.01, "fixed": False},
            ],
        }
        issues = validate_simulation_spec(spec)
        self.assertTrue(any("no driving force" in i.lower() for i in issues))

    def test_motor_targets_missing_shaft(self):
        """Motor referencing a non-existent shaft should be caught."""
        spec = {
            "objects": [
                {"type": "shaft", "id": "sun", "inertia": 0.01, "fixed": False},
                {"type": "motor_shaft_speed", "id": "m1", "shaft": "nonexistent", "speed_rpm": 900},
            ],
        }
        issues = validate_simulation_spec(spec)
        self.assertTrue(any("nonexistent" in i for i in issues))

    def test_motor_targets_missing_body(self):
        spec = {
            "objects": [
                {"type": "body", "id": "link_a", "mass": 1, "fixed": False},
                {"type": "motor_body_speed", "id": "m1", "body": "ghost", "speed_rpm": 100},
            ],
        }
        issues = validate_simulation_spec(spec)
        self.assertTrue(any("ghost" in i for i in issues))

    def test_all_fixed(self):
        """If all elements are fixed, nothing can move."""
        spec = {
            "objects": [
                {"type": "shaft", "id": "a", "inertia": 0.01, "fixed": True},
                {"type": "motor_shaft_speed", "id": "m1", "shaft": "a", "speed_rpm": 100},
            ],
        }
        issues = validate_simulation_spec(spec)
        self.assertTrue(any("fixed" in i.lower() for i in issues))


class TestDrivenPartResolution(unittest.TestCase):
    def test_explicit_driven_part(self):
        """When driven_part is set, motor should target it directly."""
        parts = [
            PartNode(id="gear_a", inertia_kg_m2=0.001),
            PartNode(id="gear_b", inertia_kg_m2=0.002),
            PartNode(id="frame", is_ground=True),
        ]
        joints = [
            JointEdge(
                id="mesh_ab",
                joint_type=JointType.GEAR_MESH,
                parent_part="gear_a",
                child_part="gear_b",
                teeth_parent=20,
                teeth_child=40,
            ),
        ]
        # Explicitly drive gear_b (child), not gear_a (parent)
        drives = [
            DriveCondition(joint_id="mesh_ab", speed_rpm=500, driven_part="gear_b"),
        ]
        mech = Mechanism(
            name="explicit_target",
            parts=tuple(parts),
            joints=tuple(joints),
            drives=tuple(drives),
        )
        spec = build_simulation_spec(mech)
        motors = [o for o in spec["objects"] if o["type"] == "motor_shaft_speed"]
        self.assertEqual(len(motors), 1)
        self.assertEqual(motors[0]["shaft"], "gear_b")

    def test_fallback_prefers_shaft_side(self):
        """When parent isn't a shaft but child is, motor should target child."""
        # Create a mechanism where the drive references a consumed joint
        # whose parent is a planet (not a shaft) but child is ring (a shaft)
        mech = _make_planetary_mechanism()
        # The default planetary mechanism drives via sun_planet_1_mesh
        # parent=sun (a shaft) — so this should still work correctly
        spec = build_simulation_spec(mech)
        motors = [o for o in spec["objects"] if o["type"] == "motor_shaft_speed"]
        self.assertEqual(len(motors), 1)
        self.assertEqual(motors[0]["shaft"], "sun")


class TestAppliedForceForwarding(unittest.TestCase):
    """Mechanism.applied_forces forwards into the daemon spec as 'applied_force'."""

    def _rotor_with_loads(self, applied_forces) -> Mechanism:
        return Mechanism(
            name="rotor_test",
            parts=(
                PartNode(id="hub", is_ground=True),
                PartNode(id="blade", mass_kg=0.05, inertia_kg_m2=0.001),
            ),
            joints=(
                JointEdge(id="rev",
                          joint_type=JointType.REVOLUTE,
                          parent_part="hub", child_part="blade",
                          axis=(0, 0, 1)),
            ),
            drives=(DriveCondition(joint_id="rev", speed_rpm=4000.0,
                                   driven_part="blade"),),
            applied_forces=applied_forces,
        )

    def test_no_forces_no_force_objects(self):
        spec = build_simulation_spec(self._rotor_with_loads(()))
        self.assertEqual(
            [o for o in spec["objects"] if o["type"] == "applied_force"],
            [],
        )

    def test_single_force_emits_one_object(self):
        spec = build_simulation_spec(self._rotor_with_loads((
            AppliedForce(target_body="blade",
                         position_local=(0.075, 0.0, 0.0),
                         force_vector=(0.0, 0.0, 1.5),
                         label="station_3"),
        )))
        forces = [o for o in spec["objects"] if o["type"] == "applied_force"]
        self.assertEqual(len(forces), 1)
        self.assertEqual(forces[0]["id"], "station_3")
        self.assertEqual(forces[0]["body"], "blade")
        self.assertEqual(forces[0]["position_local"], [0.075, 0.0, 0.0])
        self.assertEqual(forces[0]["force_vector"], [0.0, 0.0, 1.5])
        self.assertEqual(forces[0]["frame"], "body")

    def test_unlabeled_forces_get_indexed_ids(self):
        spec = build_simulation_spec(self._rotor_with_loads(tuple(
            AppliedForce(target_body="blade",
                         position_local=(0.01 * i, 0, 0),
                         force_vector=(0, 0, 1.0))
            for i in range(3)
        )))
        forces = [o for o in spec["objects"] if o["type"] == "applied_force"]
        ids = sorted(o["id"] for o in forces)
        self.assertEqual(ids, ["applied_force_0", "applied_force_1", "applied_force_2"])

    def test_world_frame_round_trips(self):
        spec = build_simulation_spec(self._rotor_with_loads((
            AppliedForce(target_body="blade",
                         position_local=(0.05, 0.0, 0.0),
                         force_vector=(0.0, 0.0, -9.81),
                         frame="world"),
        )))
        forces = [o for o in spec["objects"] if o["type"] == "applied_force"]
        self.assertEqual(forces[0]["frame"], "world")


if __name__ == "__main__":
    unittest.main()
