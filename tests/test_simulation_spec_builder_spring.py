"""TICKET-C: spring-on-prismatic emission in the simulation spec builder.

Pure-Python tests (no Chrono needed): verify a prismatic joint carrying spring
params emits a sibling ``spring`` object, and that a spring-less prismatic emits
output byte-identical to before (regression guard).
"""
from __future__ import annotations

import unittest

from server.motion_models import (
    JointEdge,
    JointType,
    Mechanism,
    PartNode,
)
from server.simulation_spec_builder import (
    build_simulation_spec,
    validate_simulation_spec,
)


def _slider(*, with_spring: bool) -> Mechanism:
    joint = JointEdge(
        id="slide",
        joint_type=JointType.PRISMATIC,
        parent_part="ground",
        child_part="plunger",
        axis=(0.0, 0.0, 1.0),
        origin=(0.0, 0.0, 0.0),
        spring_k_n_per_m=300.0 if with_spring else None,
        spring_rest_length_m=0.04 if with_spring else None,
        spring_preload_n=2.0 if with_spring else 0.0,
    )
    return Mechanism(
        name="spring_slider",
        parts=(
            PartNode(id="ground", is_ground=True),
            PartNode(id="plunger", mass_kg=0.05),
        ),
        joints=(joint,),
        drives=(),
    )


def _by_type(objects, type_name):
    return [o for o in objects if o["type"] == type_name]


class TestSpringEmission(unittest.TestCase):
    def test_spring_object_emitted_with_params(self) -> None:
        objs = build_simulation_spec(_slider(with_spring=True))["objects"]
        springs = _by_type(objs, "spring")
        self.assertEqual(len(springs), 1)
        s = springs[0]
        self.assertEqual(s["id"], "slide_spring")
        self.assertEqual(s["body_1"], "ground")
        self.assertEqual(s["body_2"], "plunger")
        self.assertEqual(s["k_n_per_m"], 300.0)
        self.assertEqual(s["rest_length_m"], 0.04)
        self.assertEqual(s["preload_n"], 2.0)
        # Direction is derived daemon-side from body positions — no pos/axis sent.
        self.assertNotIn("axis", s)
        self.assertNotIn("pos", s)

    def test_spring_only_spec_passes_validation(self) -> None:
        """A cocked spring-loaded slider has no motor; the spring is its driver."""
        spec = build_simulation_spec(_slider(with_spring=True))
        self.assertEqual(validate_simulation_spec(spec), [])

    def test_motorless_springless_spec_still_rejected(self) -> None:
        spec = build_simulation_spec(_slider(with_spring=False))
        issues = validate_simulation_spec(spec)
        self.assertTrue(any("no driving force" in i.lower() for i in issues))

    def test_spring_on_non_prismatic_warns(self) -> None:
        joint = JointEdge(
            id="hinge", joint_type=JointType.REVOLUTE,
            parent_part="ground", child_part="plunger",
            spring_k_n_per_m=300.0,
        )
        mech = Mechanism(
            name="m",
            parts=(PartNode(id="ground", is_ground=True),
                   PartNode(id="plunger", mass_kg=0.05)),
            joints=(joint,), drives=(),
        )
        with self.assertLogs("solidmind.simulation_spec_builder", level="WARNING") as cm:
            objs = build_simulation_spec(mech)["objects"]
        self.assertEqual(_by_type(objs, "spring"), [])  # not emitted
        self.assertTrue(any("only" in m and "PRISMATIC" in m for m in cm.output))

    def test_prismatic_still_emitted_alongside_spring(self) -> None:
        objs = build_simulation_spec(_slider(with_spring=True))["objects"]
        self.assertEqual(len(_by_type(objs, "prismatic")), 1)

    def test_no_spring_object_without_params(self) -> None:
        objs = build_simulation_spec(_slider(with_spring=False))["objects"]
        self.assertEqual(_by_type(objs, "spring"), [])

    def test_prismatic_byte_identical_without_spring(self) -> None:
        """Regression: a spring-less prismatic spec is unchanged from before."""
        objs = build_simulation_spec(_slider(with_spring=False))["objects"]
        prismatics = _by_type(objs, "prismatic")
        self.assertEqual(len(prismatics), 1)
        self.assertEqual(prismatics[0], {
            "type": "prismatic",
            "id": "slide",
            "body_1": "ground",
            "body_2": "plunger",
            "pos": [0.0, 0.0, 0.0],
        })

    def test_rest_length_optional(self) -> None:
        joint = JointEdge(
            id="slide",
            joint_type=JointType.PRISMATIC,
            parent_part="ground",
            child_part="plunger",
            axis=(0.0, 0.0, 1.0),
            spring_k_n_per_m=120.0,  # no rest length -> daemon uses initial separation
        )
        mech = Mechanism(
            name="m",
            parts=(PartNode(id="ground", is_ground=True),
                   PartNode(id="plunger", mass_kg=0.05)),
            joints=(joint,),
            drives=(),
        )
        s = _by_type(build_simulation_spec(mech)["objects"], "spring")[0]
        self.assertNotIn("rest_length_m", s)
        self.assertEqual(s["k_n_per_m"], 120.0)

    def test_joint_dict_round_trip_preserves_spring(self) -> None:
        j = _slider(with_spring=True).joints[0]
        restored = JointEdge.from_dict(j.to_dict())
        self.assertEqual(restored.spring_k_n_per_m, 300.0)
        self.assertEqual(restored.spring_rest_length_m, 0.04)
        self.assertEqual(restored.spring_preload_n, 2.0)

    def test_joint_dict_omits_spring_when_absent(self) -> None:
        j = _slider(with_spring=False).joints[0]
        d = j.to_dict()
        self.assertNotIn("spring_k_n_per_m", d)
        self.assertNotIn("spring_rest_length_m", d)
        self.assertNotIn("spring_preload_n", d)


if __name__ == "__main__":
    unittest.main()
