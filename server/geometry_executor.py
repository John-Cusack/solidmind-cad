from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from server.geometry_ir import CompiledOp, Notice
from server.geometry_references import ReferenceResolver
from server.jcs import canonicalize as jcs_canonicalize


@dataclass(frozen=True, slots=True)
class ExecutionStep:
    """Record of a single operation execution."""

    op: CompiledOp
    status: str = "pending"  # "pending" | "completed" | "failed" | "skipped"
    output: dict[str, Any] = field(default_factory=dict)
    notices: list[Notice] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ExecutionTrace:
    """Complete execution trace across all operations."""

    steps: list[ExecutionStep] = field(default_factory=list)
    reference_map: dict[str, str] = field(default_factory=dict)
    notices: list[Notice] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Op-type to FreeCAD command mapping
# ---------------------------------------------------------------------------

_OP_TO_CMD: dict[str, str] = {
    "cad.sketch": "sketch",
    "cad.pad": "pad",
    "cad.pocket": "pocket",
    "cad.revolution": "revolve",
    "cad.hole": "hole",
    "cad.fillet": "fillet",
    "cad.chamfer": "chamfer",
    "cad.polar_pattern": "polar_pattern",
    "cad.sweep": "sweep",
    "cad.loft": "loft",
    "cad.get_dimensions": "get_dimensions",
}


class Executor:
    """Dispatches compiled ops to a FreeCAD client (or mock).

    Supports mock client for testing (same pattern as tests/test_tools_cad.py).
    """

    def __init__(
        self,
        client: Any | None = None,
        reference_resolver: ReferenceResolver | None = None,
    ) -> None:
        self._client = client
        self._resolver = reference_resolver or ReferenceResolver()
        self._steps: list[ExecutionStep] = []
        self._notices: list[Notice] = []
        self._metadata: dict[str, Any] = {}

    @property
    def reference_map(self) -> dict[str, str]:
        return self._resolver.reference_map

    def execute_plan(
        self,
        compiled_ops: list[CompiledOp],
        backend: str = "freecad",
    ) -> ExecutionTrace:
        """Execute all compiled operations in dependency order.

        Args:
            compiled_ops: List of compiled operations to execute.
            backend: Target backend ("freecad" or "mock").

        Returns:
            ExecutionTrace with all steps, reference map, and notices.
        """
        self._steps = []
        self._notices = []
        self._metadata = {}

        for op in compiled_ops:
            step = self._execute_operation(op, backend)
            self._steps.append(step)

            # Collect step-level notices
            self._notices.extend(step.notices)

            # Register results for reference resolution
            if step.status == "completed" and step.output:
                self._resolver.register_result(op.id, step.output)

        self._metadata["checkpoint_summary"] = self._build_checkpoint_summary(self._steps)

        return ExecutionTrace(
            steps=list(self._steps),
            reference_map=self._resolver.reference_map,
            notices=list(self._notices),
            metadata=dict(self._metadata),
        )

    def _build_checkpoint_summary(self, steps: list[ExecutionStep]) -> dict[str, Any]:
        """Build deterministic phase/checkpoint telemetry summary."""
        total_steps = len(steps)
        failed_steps = sum(1 for s in steps if s.status == "failed")
        completed_steps = sum(1 for s in steps if s.status == "completed")
        phase_counts: dict[str, int] = {}
        topology_sensitive_ops: list[str] = []
        for step in steps:
            phase_id = step.op.phase_id or "UNSPECIFIED"
            phase_counts[phase_id] = phase_counts.get(phase_id, 0) + 1
            if bool(step.op.topology_sensitive):
                topology_sensitive_ops.append(step.op.op_type)

        return {
            "total_steps": total_steps,
            "completed_steps": completed_steps,
            "failed_steps": failed_steps,
            "phase_counts": {k: phase_counts[k] for k in sorted(phase_counts.keys())},
            "topology_sensitive_ops_touched": sorted(topology_sensitive_ops),
        }

    def _execute_operation(
        self,
        op: CompiledOp,
        backend: str,
    ) -> ExecutionStep:
        """Execute a single compiled operation."""
        notices: list[Notice] = []

        # Resolve any reference tokens in inputs
        resolved_inputs = self._resolve_inputs(op.inputs, notices)

        try:
            if backend == "freecad" and self._client is not None:
                output = self._execute_freecad_op(op, resolved_inputs)
            else:
                # Mock/dry-run: produce synthetic output
                output = self._mock_execute(op, resolved_inputs)

            # Check invariants against output
            inv_notices = self._check_invariants(op.invariants, output)
            notices.extend(inv_notices)

            return ExecutionStep(
                op=op,
                status="completed",
                output=output,
                notices=notices,
            )

        except Exception as e:
            notices.append(
                Notice(
                    code="EXECUTION_ERROR",
                    severity="error",
                    message=f"Error executing {op.op_type}: {e}",
                    context={"operation_id": op.id},
                )
            )
            return ExecutionStep(
                op=op,
                status="failed",
                output={"status": "error", "error": str(e)},
                notices=notices,
            )

    def _execute_freecad_op(
        self,
        op: CompiledOp,
        resolved_inputs: dict[str, Any],
    ) -> dict[str, Any]:
        """Dispatch operation to FreeCAD client via send_command."""
        cmd = _OP_TO_CMD.get(op.op_type, op.op_type)

        result = self._client.send_command(cmd, resolved_inputs)

        output: dict[str, Any] = {
            "status": "success",
            "tool": op.op_type,
        }

        if isinstance(result, dict):
            if result.get("ok") is False:
                raise RuntimeError(
                    result.get("error", {}).get("message", "Unknown FreeCAD error")
                )
            output["result"] = result.get("result", result)
            result_name = result.get("result", {}).get("name", "")
            if result_name:
                output["result_name"] = result_name
        else:
            output["result"] = result

        return output

    def _mock_execute(
        self,
        op: CompiledOp,
        resolved_inputs: dict[str, Any],
    ) -> dict[str, Any]:
        """Produce synthetic output for testing without FreeCAD."""
        output: dict[str, Any] = {
            "status": "success",
            "tool": op.op_type,
            "mock": True,
        }

        # Produce predictable mock outputs for reference resolution
        if op.op_type in ("cad.pad", "pad"):
            output["result_name"] = "Pad"
        elif op.op_type in ("cad.sketch", "create_sketch"):
            output["result_name"] = "Sketch"
        elif op.op_type in ("cad.hole", "hole"):
            output["result_name"] = "Hole"
        elif op.op_type in ("cad.fillet", "fillet"):
            output["result_name"] = "Fillet"
        elif op.op_type in ("cad.chamfer", "chamfer"):
            output["result_name"] = "Chamfer"
        elif op.op_type in ("cad.sweep", "sweep"):
            output["result_name"] = "AdditivePipe"
        elif op.op_type in ("cad.loft", "loft"):
            output["result_name"] = "AdditiveLoft"

        return output

    def _resolve_inputs(
        self,
        inputs: dict[str, Any],
        notices: list[Notice],
    ) -> dict[str, Any]:
        """Resolve reference tokens within operation inputs."""
        resolved = dict(inputs)

        # Resolve face references
        face = resolved.get("face", "")
        if isinstance(face, str) and face.startswith("ref:"):
            ref_result = self._resolver.resolve_face_ref(face)
            if ref_result.status == "resolved" and ref_result.selected:
                resolved["face"] = ref_result.selected
            notices.extend(ref_result.notices)

        # Resolve edge references
        edges = resolved.get("edges", [])
        if isinstance(edges, list):
            resolved_edges: list[str] = []
            for edge in edges:
                if isinstance(edge, str) and edge.startswith("ref:"):
                    ref_result = self._resolver.resolve_face_ref(edge)
                    if ref_result.status == "resolved" and ref_result.selected:
                        resolved_edges.append(ref_result.selected)
                    notices.extend(ref_result.notices)
                else:
                    resolved_edges.append(str(edge))
            resolved["edges"] = resolved_edges

        return resolved

    def _check_invariants(
        self,
        invariants: list[Any],
        output: dict[str, Any],
    ) -> list[Notice]:
        """Check operation invariants against execution output."""
        notices: list[Notice] = []

        for inv in invariants:
            inv_type = inv.type if hasattr(inv, "type") else str(inv)

            if inv_type == "solid_created":
                if output.get("status") != "success":
                    notices.append(
                        Notice(
                            code="INVARIANT_FAILED_SOLID_CREATED",
                            severity="warning",
                            message="Solid creation invariant not met",
                        )
                    )
            elif inv_type == "sketch_valid":
                if output.get("status") != "success":
                    notices.append(
                        Notice(
                            code="INVARIANT_FAILED_SKETCH_VALID",
                            severity="warning",
                            message="Sketch validity invariant not met",
                        )
                    )

        return notices


def compute_execution_trace_hash(trace: ExecutionTrace) -> str:
    """Compute deterministic hash of execution trace per Section 8."""
    steps_data = []
    for step in trace.steps:
        steps_data.append({
            "op_id": step.op.id,
            "op_type": step.op.op_type,
            "status": step.status,
            "output_keys": sorted(step.output.keys()) if step.output else [],
        })

    canonical = {
        "steps": steps_data,
        "reference_map": {k: v for k, v in sorted(trace.reference_map.items())},
    }
    canonical_str = jcs_canonicalize(canonical)
    return hashlib.sha256(canonical_str.encode()).hexdigest()
