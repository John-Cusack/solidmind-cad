from __future__ import annotations

from typing import Any

from server.feature_support import load_geometry_capabilities, load_planning_policy
from server.geometry_constraints import ConstraintGraph, ConstraintGraphBuilder
from server.geometry_ir import (
    EIR,
    GIR,
    EIRBuilder,
    GIRBuilder,
    Invariant,
    Point3D,
    Quantity,
    Vector3D,
    compute_eir_hash,
    compute_gir_hash,
)
from server.geometry_planner import StrategyPlanner
from server.planning_classifier import classify_archetype
from server.planning_questions import evaluate_planning_question_budget
from server.planning_types import (
    PlanningCheckpoint,
    PlanningContext,
    PlanningOperation,
    PlanningPhase,
    PlanningPlan,
    PlanningQuestionBudget,
    RepairDirective,
    compute_planning_plan_hash,
    planning_plan_to_dict,
)
from server.spec_planning_context import normalize_spec_for_planning

_PHASE_GOALS: dict[str, str] = {
    "BASE": "Create datum frame, master parameters, and base envelope geometry.",
    "INTERFACES": "Create critical interfaces and functional references.",
    "STRUCTURE": "Create major cuts, cavities, shells, and structural features.",
    "PATTERNS": "Create replicated features only after seed stabilization.",
    "FINISH": "Apply topology-sensitive finish operations such as fillets/chamfers.",
}


def plan_geometry(spec: dict[str, Any], options: dict[str, Any] | None = None) -> dict[str, Any]:
    """Plan geometry from finalized spec using generic geometry engine.

    options:
      - planning_mode: legacy | policy_v1
      - strict_mode: bool (reserved)
      - question_budget_override: int
    """
    opts = options or {}
    planning_mode = str(opts.get("planning_mode", "legacy"))

    if planning_mode == "policy_v1":
        return _plan_geometry_policy_v1(spec, opts)

    return _plan_geometry_legacy(spec)


def _plan_geometry_legacy(spec: dict[str, Any]) -> dict[str, Any]:
    notices: list[dict[str, Any]] = []
    metadata: dict[str, Any] = {
        "engine_mode": "generic_v1",
        "engine_version": "1.0",
        "planning_mode": "legacy",
    }

    # Step 1: Build constraint graph from spec
    cg_builder = ConstraintGraphBuilder()
    constraint_graph = cg_builder.build_from_spec(spec)

    # Step 2: Extract GIR from spec + constraints
    gir = _extract_gir(spec, constraint_graph, notices)
    gir_hash = compute_gir_hash(gir)
    metadata["gir_hash"] = gir_hash

    # Step 3: Select strategy
    gir_dict = _gir_to_dict(gir)
    capabilities = load_geometry_capabilities()
    planner = StrategyPlanner(capabilities)
    strategies = planner.select_strategy(gir_dict, backend="freecad")
    metadata["strategy"] = strategies.primary.strategy_name
    metadata["strategy_confidence"] = strategies.primary.confidence

    # Step 4: Generate EIR from GIR + strategy
    eir = _generate_eir(gir, strategies.primary.strategy_name, notices)
    eir_hash = compute_eir_hash(eir)
    metadata["eir_hash"] = eir_hash

    eir_dict = _eir_to_dict(eir)

    return {
        "gir": gir_dict,
        "eir": eir_dict,
        "notices": notices,
        "metadata": metadata,
    }


def _plan_geometry_policy_v1(spec: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
    notices: list[dict[str, Any]] = []
    metadata: dict[str, Any] = {
        "engine_mode": "generic_v1",
        "engine_version": "1.0",
        "planning_mode": "policy_v1",
    }

    normalized = normalize_spec_for_planning(spec)
    classifier = classify_archetype(normalized)
    metadata["process"] = classifier.process
    metadata["archetype"] = classifier.archetype
    metadata["archetype_scores"] = classifier.scores

    policy_manifest = load_planning_policy()
    question_budget_default = policy_manifest.default_question_budget
    question_budget_override = options.get("question_budget_override")
    if isinstance(question_budget_override, bool) or not isinstance(question_budget_override, int):
        question_budget = question_budget_default
    else:
        question_budget = max(0, question_budget_override)

    policy_key = _select_policy_key(policy_manifest, classifier.process, classifier.archetype)
    metadata["policy_key"] = policy_key

    policy = policy_manifest.policies.get(policy_key)
    if policy is None:
        notices.append(
            {
                "code": "PLANNING_POLICY_MISSING",
                "severity": "warning",
                "message": f"No exact policy found for process={classifier.process}, archetype={classifier.archetype}; falling back to legacy planning.",
            }
        )
        legacy = _plan_geometry_legacy(normalized)
        legacy["metadata"]["planning_mode"] = "policy_v1_fallback_legacy"
        legacy["metadata"]["policy_key"] = policy_key
        legacy["notices"] = notices + list(legacy.get("notices", []))
        return legacy

    budget_result = evaluate_planning_question_budget(
        normalized,
        process=classifier.process,
        archetype=classifier.archetype,
        max_questions=question_budget,
    )

    planning_context = PlanningContext(
        process=classifier.process,
        archetype=classifier.archetype,
        policy_key=policy_key,
        units=str(normalized.get("units", "mm")),
        normalized_spec=normalized,
    )

    # Step 1: Build constraint graph from normalized spec
    cg_builder = ConstraintGraphBuilder()
    constraint_graph = cg_builder.build_from_spec(normalized)

    # Step 2: Extract GIR
    gir = _extract_gir(
        normalized,
        constraint_graph,
        notices,
        planning_context=planning_context,
    )
    gir_hash = compute_gir_hash(gir)
    metadata["gir_hash"] = gir_hash

    # Step 3: Strategy
    gir_dict = _gir_to_dict(gir)
    capabilities = load_geometry_capabilities()
    planner = StrategyPlanner(capabilities)
    strategies = planner.select_strategy(gir_dict, backend="freecad")
    metadata["strategy"] = strategies.primary.strategy_name
    metadata["strategy_confidence"] = strategies.primary.confidence

    # Step 4: EIR
    eir = _generate_eir(gir, strategies.primary.strategy_name, notices)
    eir_hash = compute_eir_hash(eir)
    metadata["eir_hash"] = eir_hash
    eir_dict = _eir_to_dict(eir)

    plan = _build_planning_plan(
        policy=policy,
        policy_key=policy_key,
        context=planning_context,
        eir_dict=eir_dict,
        assumptions=budget_result.assumptions,
        questions_asked=budget_result.questions_asked,
        max_questions=budget_result.max_questions,
    )
    planning_plan = planning_plan_to_dict(plan)
    planning_plan_hash = compute_planning_plan_hash(plan)

    metadata["planning_plan_hash"] = planning_plan_hash
    metadata["question_budget"] = {
        "max_questions": budget_result.max_questions,
        "questions_asked_count": len(budget_result.questions_asked),
    }

    return {
        "gir": gir_dict,
        "eir": eir_dict,
        "planning_plan": planning_plan,
        "planning_plan_hash": planning_plan_hash,
        "policy_key": policy_key,
        "archetype": classifier.archetype,
        "assumptions": budget_result.assumptions,
        "question_budget": {
            "max_questions": budget_result.max_questions,
            "questions_asked": budget_result.questions_asked,
        },
        "notices": notices,
        "metadata": metadata,
    }


def _select_policy_key(policy_manifest: Any, process: str, archetype: str) -> str:
    exact = f"{process}_{archetype}"
    if exact in policy_manifest.policies:
        return exact

    fallback_candidates = [
        f"{process}_prismatic",
        f"{process}_revolved",
        "cnc_prismatic",
    ]
    for key in fallback_candidates:
        if key in policy_manifest.policies:
            return key

    # deterministic stable fallback to first key
    if policy_manifest.policies:
        return sorted(policy_manifest.policies.keys())[0]
    return exact


def _build_planning_plan(
    *,
    policy: Any,
    policy_key: str,
    context: PlanningContext,
    eir_dict: dict[str, Any],
    assumptions: list[str],
    questions_asked: list[str],
    max_questions: int,
) -> PlanningPlan:
    phase_ops: dict[str, list[PlanningOperation]] = {}
    for op in eir_dict.get("operations", []):
        if not isinstance(op, dict):
            continue
        phase_id = str(op.get("phase_id") or _phase_for_op_type(str(op.get("op_type", ""))))
        phase_ops.setdefault(phase_id, []).append(
            PlanningOperation(
                op_id=str(op.get("id", "")),
                op_type=str(op.get("op_type", "")),
                reference_support_type=(
                    str(op.get("reference_support_type"))
                    if op.get("reference_support_type") is not None
                    else None
                ),
                topology_sensitive=bool(op.get("topology_sensitive", False)),
            )
        )

    phases: list[PlanningPhase] = []
    for phase_id in policy.phase_order:
        phase_policy = policy.phase_policies.get(phase_id)
        checkpoints: list[PlanningCheckpoint] = []
        if phase_policy is not None:
            checkpoints = list(phase_policy.checkpoints)
        phases.append(
            PlanningPhase(
                phase_id=phase_id,
                goal=_PHASE_GOALS.get(phase_id, phase_id),
                checkpoints=checkpoints,
                operations=phase_ops.get(phase_id, []),
            )
        )

    directives = [
        RepairDirective(
            playbook_id=pb.playbook_id,
            trigger=pb.trigger,
            actions=list(pb.steps),
        )
        for pb in policy.repair_playbooks
    ]

    return PlanningPlan(
        plan_version="1.0",
        policy_key=policy_key,
        process=context.process,
        archetype=context.archetype,
        question_budget=PlanningQuestionBudget(
            max_questions=max_questions,
            questions_asked=list(questions_asked),
        ),
        assumptions=list(assumptions),
        phases=phases,
        repair_directives=directives,
    )


def _phase_for_feature_type(feature_type: str) -> str:
    if feature_type in ("primitive", "sketch_profile", "extrude_intent", "revolve_intent"):
        return "BASE"
    if feature_type in ("hole_intent",):
        return "INTERFACES"
    if feature_type in ("sweep_intent", "loft_intent"):
        return "STRUCTURE"
    if feature_type in ("pattern_intent",):
        return "PATTERNS"
    if feature_type in ("blend_intent",):
        return "FINISH"
    return "STRUCTURE"


def _phase_for_op_type(op_type: str) -> str:
    if op_type in ("create_sketch", "pad", "revolve"):
        return "BASE"
    if op_type in ("hole",):
        return "INTERFACES"
    if op_type in ("sweep", "loft", "pocket"):
        return "STRUCTURE"
    if op_type in ("polar_pattern",):
        return "PATTERNS"
    if op_type in ("fillet", "chamfer"):
        return "FINISH"
    return "STRUCTURE"


def _extract_gir(
    spec: dict[str, Any],
    constraint_graph: ConstraintGraph,
    notices: list[dict[str, Any]],
    planning_context: PlanningContext | None = None,
) -> GIR:
    """Extract GIR from spec geometry fields and constraint graph."""
    del constraint_graph  # reserved for future policy-aware extraction

    gir_builder = GIRBuilder()
    frame_id = gir_builder.add_global_frame()

    envelope = spec.get("envelope", {})
    geometry = spec.get("geometry", {})

    # Extract envelope dimensions
    dims = _extract_envelope_dims(envelope)

    has_envelope = bool(dims)

    if has_envelope:
        sketch_phase = _phase_for_feature_type("sketch_profile")
        sketch = _add_box_sketch(gir_builder, dims, frame_id, phase_id=sketch_phase)

        # Create extrude intent from sketch + height
        height = dims.get("height")
        if height is None:
            height = Quantity(value=10.0, unit="mm")
            notices.append(
                {
                    "code": "DEFAULT_HEIGHT",
                    "severity": "info",
                    "message": "No height specified, using default 10mm",
                }
            )

        gir_builder.add_extrude_intent(
            base_profile_id=sketch.id,
            distance=height,
            operation_type="add",
            frame_id=frame_id,
            traces_to=["envelope"],
            phase_id=_phase_for_feature_type("extrude_intent"),
            reference_support_type="origin",
            topology_sensitive=False,
        )

        hole_features = geometry.get("hole_features", [])
        _extract_holes(
            gir_builder,
            hole_features,
            sketch.id,
            frame_id,
            notices,
            phase_id=_phase_for_feature_type("hole_intent"),
        )

        blend_specs = geometry.get("fillets", [])
        chamfer_specs = geometry.get("chamfers", [])
        _extract_blends(
            gir_builder,
            blend_specs,
            chamfer_specs,
            sketch.id,
            frame_id,
            notices,
            phase_id=_phase_for_feature_type("blend_intent"),
        )
    else:
        notices.append(
            {
                "code": "NO_ENVELOPE",
                "severity": "warning",
                "message": "No envelope dimensions found in spec",
            }
        )

    if planning_context is not None:
        gir_builder.set_metadata("process", planning_context.process)
        gir_builder.set_metadata("archetype", planning_context.archetype)
        gir_builder.set_metadata("policy_key", planning_context.policy_key)
    gir_builder.set_metadata("source", "spec")
    return gir_builder.build()


def _extract_envelope_dims(envelope: dict[str, Any]) -> dict[str, Quantity]:
    """Extract envelope dimensions as Quantity objects."""
    dims: dict[str, Quantity] = {}
    for dim_name in ["length", "width", "height"]:
        dim_value = envelope.get(dim_name)
        if isinstance(dim_value, dict):
            value = dim_value.get("value")
            unit = dim_value.get("unit", "mm")
            if value is not None:
                dims[dim_name] = Quantity(value=float(value), unit=unit)
    return dims


def _add_box_sketch(
    gir_builder: GIRBuilder,
    dims: dict[str, Quantity],
    frame_id: str,
    phase_id: str | None = None,
) -> Any:
    """Add a rectangular sketch profile from envelope dimensions."""
    length = dims.get("length", Quantity(value=100.0, unit="mm"))
    width = dims.get("width", length)

    lv = length.value
    wv = width.value
    elements: list[dict[str, Any]] = [
        {
            "type": "rect",
            "x": -lv / 2,
            "y": -wv / 2,
            "width": lv,
            "height": wv,
        }
    ]

    constraints: list[dict[str, Any]] = [
        {"type": "Symmetric", "axis": "X"},
        {"type": "Symmetric", "axis": "Y"},
    ]

    return gir_builder.add_sketch_profile(
        plane="XY",
        elements=elements,
        constraints=constraints,
        frame_id=frame_id,
        traces_to=["envelope"],
        phase_id=phase_id,
        reference_support_type="origin",
        topology_sensitive=False,
    )


def _extract_holes(
    gir_builder: GIRBuilder,
    hole_features: list[dict[str, Any]],
    parent_feature_id: str,
    frame_id: str,
    notices: list[dict[str, Any]],
    phase_id: str | None = None,
) -> list[Any]:
    """Extract hole intents from spec hole_features."""
    hole_intents = []
    for hole_spec in hole_features:
        if not isinstance(hole_spec, dict):
            continue

        hole_id = str(hole_spec.get("id", "hole"))

        diam = hole_spec.get("diameter", {})
        if not isinstance(diam, dict) or diam.get("value") is None:
            notices.append(
                {
                    "code": "MISSING_HOLE_DIAMETER",
                    "severity": "warning",
                    "message": f"Hole '{hole_id}' missing diameter, skipping",
                }
            )
            continue
        diameter = Quantity(value=float(diam["value"]), unit=diam.get("unit", "mm"))

        depth_spec = hole_spec.get("depth", {})
        if isinstance(depth_spec, dict) and depth_spec.get("value") is not None:
            depth = Quantity(
                value=float(depth_spec["value"]),
                unit=depth_spec.get("unit", "mm"),
            )
        else:
            depth = Quantity(value=0.0, unit="mm")

        hole_type = str(hole_spec.get("type", "simple"))
        if depth.value == 0.0:
            hole_type = "through"

        loc = hole_spec.get("location", {})
        location = _parse_location(loc)

        face_ref = f"ref:{parent_feature_id}:top_face"

        intent = gir_builder.add_hole_intent(
            diameter=diameter,
            depth=depth,
            hole_type=hole_type,
            location=location,
            face_reference=face_ref,
            axis=Vector3D(x=0.0, y=0.0, z=-1.0),
            frame_id=frame_id,
            traces_to=[f"hole:{hole_id}"],
            phase_id=phase_id,
            reference_support_type="datum",
            topology_sensitive=False,
        )
        hole_intents.append(intent)

    return hole_intents


def _parse_location(loc: dict[str, Any]) -> Point3D:
    """Parse a location dict into a Point3D."""

    def _coord(key: str) -> Quantity:
        val = loc.get(key, {})
        if isinstance(val, dict) and val.get("value") is not None:
            return Quantity(value=float(val["value"]), unit=val.get("unit", "mm"))
        if isinstance(val, (int, float)):
            return Quantity(value=float(val), unit="mm")
        return Quantity(value=0.0, unit="mm")

    return Point3D(x=_coord("x"), y=_coord("y"), z=_coord("z"))


def _extract_blends(
    gir_builder: GIRBuilder,
    fillet_specs: list[dict[str, Any]],
    chamfer_specs: list[dict[str, Any]],
    parent_feature_id: str,
    frame_id: str,
    notices: list[dict[str, Any]],
    phase_id: str | None = None,
) -> None:
    """Extract blend intents (fillets and chamfers) from spec."""
    for fillet in fillet_specs:
        if not isinstance(fillet, dict):
            continue
        radius = fillet.get("radius", {})
        if isinstance(radius, dict) and radius.get("value") is not None:
            r = Quantity(value=float(radius["value"]), unit=radius.get("unit", "mm"))
        elif isinstance(radius, (int, float)):
            r = Quantity(value=float(radius), unit="mm")
        else:
            notices.append(
                {
                    "code": "MISSING_FILLET_RADIUS",
                    "severity": "warning",
                    "message": "Fillet missing radius, skipping",
                }
            )
            continue

        edge_refs = _get_edge_refs(fillet, parent_feature_id)
        gir_builder.add_blend_intent(
            blend_type="fillet",
            edge_references=edge_refs,
            radius=r,
            frame_id=frame_id,
            traces_to=["blend:fillet"],
            phase_id=phase_id,
            reference_support_type="edge",
            topology_sensitive=True,
        )

    for chamfer in chamfer_specs:
        if not isinstance(chamfer, dict):
            continue
        dist = chamfer.get("distance", chamfer.get("size", {}))
        if isinstance(dist, dict) and dist.get("value") is not None:
            d = Quantity(value=float(dist["value"]), unit=dist.get("unit", "mm"))
        elif isinstance(dist, (int, float)):
            d = Quantity(value=float(dist), unit="mm")
        else:
            notices.append(
                {
                    "code": "MISSING_CHAMFER_SIZE",
                    "severity": "warning",
                    "message": "Chamfer missing size, skipping",
                }
            )
            continue

        edge_refs = _get_edge_refs(chamfer, parent_feature_id)
        gir_builder.add_blend_intent(
            blend_type="chamfer",
            edge_references=edge_refs,
            distance=d,
            frame_id=frame_id,
            traces_to=["blend:chamfer"],
            phase_id=phase_id,
            reference_support_type="edge",
            topology_sensitive=True,
        )


def _get_edge_refs(blend_spec: dict[str, Any], parent_feature_id: str) -> list[str]:
    """Extract edge references from a blend spec."""
    edges = blend_spec.get("edges", [])
    if edges:
        return [str(e) for e in edges]
    edge_selector = blend_spec.get("edge_selector", "vertical")
    return [f"ref:{parent_feature_id}:{edge_selector}_edges"]


# ---------------------------------------------------------------------------
# EIR generation
# ---------------------------------------------------------------------------

def _generate_eir(
    gir: GIR,
    strategy: str,
    notices: list[dict[str, Any]],
) -> EIR:
    """Translate GIR feature graph to ordered EIR operation DAG."""
    del notices

    eir_builder = EIRBuilder()
    eir_builder.set_metadata("strategy", strategy)

    sketch_op_ids: dict[str, str] = {}
    solid_op_ids: dict[str, str] = {}

    for feature in gir.features:
        if feature.type == "sketch_profile":
            op = eir_builder.add_operation(
                op_type="create_sketch",
                inputs={
                    "body": "Body",
                    "plane": feature.plane,
                    "elements": feature.elements,
                    "constraints": feature.constraints,
                },
                invariants=[Invariant(type="sketch_valid", scope="local")],
                feature_provenance_id=feature.id,
                phase_id=feature.phase_id or _phase_for_feature_type(feature.type),
                reference_support_type=feature.reference_support_type,
                topology_sensitive=bool(feature.topology_sensitive),
            )
            sketch_op_ids[feature.id] = op.id

        elif feature.type == "extrude_intent":
            depends = []
            sketch_op = sketch_op_ids.get(feature.base_profile_id)
            if sketch_op:
                depends.append(sketch_op)

            op = eir_builder.add_operation(
                op_type="pad",
                inputs={
                    "sketch": "Sketch",
                    "length": feature.distance.value,
                },
                depends_on=depends,
                invariants=[
                    Invariant(type="solid_created", scope="global"),
                    Invariant(type="dimension_check", threshold=feature.distance.value, scope="z_extent"),
                ],
                feature_provenance_id=feature.id,
                phase_id=feature.phase_id or _phase_for_feature_type(feature.type),
                reference_support_type=feature.reference_support_type,
                topology_sensitive=bool(feature.topology_sensitive),
            )
            solid_op_ids[feature.id] = op.id

        elif feature.type == "hole_intent":
            depends = list(solid_op_ids.values())

            eir_builder.add_operation(
                op_type="hole",
                inputs={
                    "face": feature.face_reference or "Face6",
                    "diameter": feature.diameter.value,
                    "depth": feature.depth.value,
                    "hole_type": "ThroughAll" if feature.hole_type == "through" else "Dimension",
                },
                depends_on=depends,
                invariants=[
                    Invariant(type="hole_diameter", threshold=feature.diameter.value, scope="local"),
                    Invariant(type="hole_depth_ratio", threshold=(feature.depth.value / feature.diameter.value) if feature.diameter.value else None, scope="local"),
                ],
                feature_provenance_id=feature.id,
                phase_id=feature.phase_id or _phase_for_feature_type(feature.type),
                reference_support_type=feature.reference_support_type,
                topology_sensitive=bool(feature.topology_sensitive),
            )

        elif feature.type == "blend_intent":
            depends = list(solid_op_ids.values())

            if feature.blend_type == "fillet":
                eir_builder.add_operation(
                    op_type="fillet",
                    inputs={
                        "radius": feature.radius.value if feature.radius else 1.0,
                        "edges": feature.edge_references,
                    },
                    depends_on=depends,
                    invariants=[Invariant(type="blend_applied", scope="local")],
                    feature_provenance_id=feature.id,
                    phase_id=feature.phase_id or _phase_for_feature_type(feature.type),
                    reference_support_type=feature.reference_support_type,
                    topology_sensitive=True,
                )
            elif feature.blend_type == "chamfer":
                eir_builder.add_operation(
                    op_type="chamfer",
                    inputs={
                        "size": feature.distance.value if feature.distance else 1.0,
                        "edges": feature.edge_references,
                    },
                    depends_on=depends,
                    invariants=[Invariant(type="blend_applied", scope="local")],
                    feature_provenance_id=feature.id,
                    phase_id=feature.phase_id or _phase_for_feature_type(feature.type),
                    reference_support_type=feature.reference_support_type,
                    topology_sensitive=True,
                )

        elif feature.type == "revolve_intent":
            depends = []
            sketch_op = sketch_op_ids.get(feature.base_profile_id)
            if sketch_op:
                depends.append(sketch_op)

            axis_map = {(0, 1, 0): "V", (1, 0, 0): "H", (0, 0, 1): "Base_Z"}
            axis_key = (int(feature.axis.x), int(feature.axis.y), int(feature.axis.z))
            axis_name = axis_map.get(axis_key, "V")

            op = eir_builder.add_operation(
                op_type="revolve",
                inputs={
                    "sketch": "Sketch",
                    "axis": axis_name,
                    "angle": feature.angle.value,
                },
                depends_on=depends,
                invariants=[Invariant(type="solid_created", scope="global")],
                feature_provenance_id=feature.id,
                phase_id=feature.phase_id or _phase_for_feature_type(feature.type),
                reference_support_type=feature.reference_support_type,
                topology_sensitive=bool(feature.topology_sensitive),
            )
            solid_op_ids[feature.id] = op.id

        elif feature.type == "sweep_intent":
            depends = []
            profile_op = sketch_op_ids.get(feature.profile_id)
            if profile_op:
                depends.append(profile_op)
            spine_op = sketch_op_ids.get(feature.spine_id)
            if spine_op:
                depends.append(spine_op)

            subtractive = feature.operation_type == "cut"

            op = eir_builder.add_operation(
                op_type="sweep",
                inputs={
                    "profile_sketch": "Sketch",
                    "spine_sketch": "Sketch",
                    "subtractive": subtractive,
                },
                depends_on=depends,
                invariants=[Invariant(type="solid_created", scope="global")],
                feature_provenance_id=feature.id,
                phase_id=feature.phase_id or _phase_for_feature_type(feature.type),
                reference_support_type=feature.reference_support_type,
                topology_sensitive=bool(feature.topology_sensitive),
            )
            solid_op_ids[feature.id] = op.id

        elif feature.type == "loft_intent":
            depends = []
            for sid in feature.section_ids:
                sec_op = sketch_op_ids.get(sid)
                if sec_op:
                    depends.append(sec_op)

            subtractive = feature.operation_type == "cut"

            op = eir_builder.add_operation(
                op_type="loft",
                inputs={
                    "sketches": ["Sketch" for _ in feature.section_ids],
                    "ruled": feature.ruled,
                    "closed": feature.closed,
                    "subtractive": subtractive,
                },
                depends_on=depends,
                invariants=[Invariant(type="solid_created", scope="global")],
                feature_provenance_id=feature.id,
                phase_id=feature.phase_id or _phase_for_feature_type(feature.type),
                reference_support_type=feature.reference_support_type,
                topology_sensitive=bool(feature.topology_sensitive),
            )
            solid_op_ids[feature.id] = op.id

        elif feature.type == "pattern_intent":
            depends = []
            for fid in feature.feature_ids:
                if fid in solid_op_ids:
                    depends.append(solid_op_ids[fid])

            eir_builder.add_operation(
                op_type="polar_pattern",
                inputs={
                    "features": feature.feature_ids,
                    "occurrences": feature.count,
                    "axis": "Base_Z",
                    "angle": feature.total_angle.value if feature.total_angle else 360.0,
                },
                depends_on=depends,
                invariants=[Invariant(type="pattern_count", threshold=float(feature.count), scope="global")],
                feature_provenance_id=feature.id,
                phase_id=feature.phase_id or _phase_for_feature_type(feature.type),
                reference_support_type=feature.reference_support_type,
                topology_sensitive=bool(feature.topology_sensitive),
            )

        elif feature.type == "primitive":
            dims = feature.dimensions
            if feature.primitive_type == "box" and dims:
                length = dims.get("length", Quantity(100.0, "mm"))
                width = dims.get("width", length)
                height = dims.get("height", Quantity(10.0, "mm"))

                sketch_op = eir_builder.add_operation(
                    op_type="create_sketch",
                    inputs={
                        "body": "Body",
                        "plane": "XY",
                        "elements": [
                            {
                                "type": "rect",
                                "x": -length.value / 2,
                                "y": -width.value / 2,
                                "width": length.value,
                                "height": width.value,
                            }
                        ],
                    },
                    feature_provenance_id=feature.id,
                    phase_id=feature.phase_id or _phase_for_feature_type(feature.type),
                    reference_support_type=feature.reference_support_type,
                    topology_sensitive=bool(feature.topology_sensitive),
                )

                pad_op = eir_builder.add_operation(
                    op_type="pad",
                    inputs={"sketch": "Sketch", "length": height.value},
                    depends_on=[sketch_op.id],
                    feature_provenance_id=feature.id,
                    phase_id=feature.phase_id or _phase_for_feature_type(feature.type),
                    reference_support_type=feature.reference_support_type,
                    topology_sensitive=bool(feature.topology_sensitive),
                )
                solid_op_ids[feature.id] = pad_op.id

    return eir_builder.build()


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def _gir_to_dict(gir: GIR) -> dict[str, Any]:
    """Serialize GIR to dict for JSON response."""
    frames = []
    for frame in gir.frames:
        frames.append({"id": frame.id, "type": frame.type, "parent": frame.parent})

    features = []
    for f in gir.features:
        feat: dict[str, Any] = {"id": f.id, "type": f.type}
        if hasattr(f, "traces_to") and f.traces_to:
            feat["traces_to"] = list(f.traces_to)
        if getattr(f, "phase_id", None) is not None:
            feat["phase_id"] = f.phase_id
        if getattr(f, "reference_support_type", None) is not None:
            feat["reference_support_type"] = f.reference_support_type
        if getattr(f, "topology_sensitive", None) is not None:
            feat["topology_sensitive"] = bool(f.topology_sensitive)

        if f.type == "sketch_profile":
            feat["plane"] = f.plane
            feat["elements"] = f.elements
        elif f.type == "extrude_intent":
            feat["base_profile_id"] = f.base_profile_id
            feat["distance"] = {"value": f.distance.value, "unit": f.distance.unit}
            feat["operation_type"] = f.operation_type
        elif f.type == "hole_intent":
            feat["diameter"] = {"value": f.diameter.value, "unit": f.diameter.unit}
            feat["depth"] = {"value": f.depth.value, "unit": f.depth.unit}
            feat["hole_type"] = f.hole_type
            feat["face_reference"] = f.face_reference
        elif f.type == "blend_intent":
            feat["blend_type"] = f.blend_type
            feat["edge_references"] = f.edge_references
            if f.radius:
                feat["radius"] = {"value": f.radius.value, "unit": f.radius.unit}
            if f.distance:
                feat["distance"] = {"value": f.distance.value, "unit": f.distance.unit}
        elif f.type == "revolve_intent":
            feat["base_profile_id"] = f.base_profile_id
            feat["angle"] = {"value": f.angle.value, "unit": f.angle.unit}
        elif f.type == "sweep_intent":
            feat["profile_id"] = f.profile_id
            feat["spine_id"] = f.spine_id
            feat["operation_type"] = f.operation_type
        elif f.type == "loft_intent":
            feat["section_ids"] = list(f.section_ids)
            feat["operation_type"] = f.operation_type
            feat["ruled"] = f.ruled
            feat["closed"] = f.closed
        elif f.type == "pattern_intent":
            feat["pattern_type"] = f.pattern_type
            feat["count"] = f.count
            feat["feature_ids"] = f.feature_ids
        elif f.type == "primitive":
            feat["primitive_type"] = f.primitive_type
            feat["dimensions"] = {
                k: {"value": v.value, "unit": v.unit} for k, v in f.dimensions.items()
            }
        features.append(feat)

    return {
        "gir_version": gir.gir_version,
        "frames": frames,
        "features": features,
        "metadata": dict(gir.metadata),
    }


def _eir_to_dict(eir: EIR) -> dict[str, Any]:
    """Serialize EIR to dict for JSON response."""
    operations = []
    for op in eir.operations:
        op_dict: dict[str, Any] = {
            "id": op.id,
            "op_type": op.op_type,
            "inputs": op.inputs,
            "depends_on": op.depends_on,
        }
        if op.invariants:
            op_dict["invariants"] = [
                {"type": inv.type, "threshold": inv.threshold, "scope": inv.scope}
                for inv in op.invariants
            ]
        if op.feature_provenance_id:
            op_dict["feature_provenance_id"] = op.feature_provenance_id
        if op.phase_id is not None:
            op_dict["phase_id"] = op.phase_id
        if op.reference_support_type is not None:
            op_dict["reference_support_type"] = op.reference_support_type
        if op.topology_sensitive is not None:
            op_dict["topology_sensitive"] = bool(op.topology_sensitive)
        operations.append(op_dict)

    return {
        "eir_version": eir.eir_version,
        "operations": operations,
        "dependency_graph": dict(eir.dependency_graph),
        "metadata": dict(eir.metadata),
    }
