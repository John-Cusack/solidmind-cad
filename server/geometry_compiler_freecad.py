from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from server.geometry_ir import (
    CompilerResult,
    CompilerStatus,
    CompiledOp,
    EIR,
    Invariant,
    Notice,
)
from server.feature_support import BackendCapabilities, load_geometry_capabilities


class FreeCADCompiler:
    def __init__(self, capabilities: BackendCapabilities | None = None) -> None:
        self._capabilities = capabilities or load_geometry_capabilities().backends.get(
            "freecad"
        )

    def compile_eir(self, eir: EIR) -> CompilerResult:
        if not self._capabilities:
            return CompilerResult(
                status=CompilerStatus.UNSUPPORTED,
                ops=None,
                notices=[
                    Notice(
                        code="BACKEND_NOT_FOUND",
                        severity="error",
                        message="FreeCAD backend capabilities not found",
                    )
                ],
            )

        compiled_ops: list[CompiledOp] = []
        all_notices: list[Notice] = []

        for operation in eir.operations:
            result = self._compile_operation(operation)

            if result.status == CompilerStatus.COMPILED and result.ops:
                compiled_ops.extend(result.ops)
            elif result.status == CompilerStatus.LOWERED and result.ops:
                compiled_ops.extend(result.ops)
                all_notices.extend(result.notices)
            elif result.status == CompilerStatus.UNSUPPORTED:
                all_notices.extend(result.notices)

        final_status = CompilerStatus.COMPILED
        if any(n.severity in ["error", "critical"] for n in all_notices):
            final_status = CompilerStatus.UNSUPPORTED
        elif any(n.severity == "warning" for n in all_notices):
            final_status = CompilerStatus.LOWERED

        return CompilerResult(
            status=final_status,
            ops=compiled_ops if compiled_ops else None,
            notices=all_notices,
        )

    def _compile_operation(self, op: CompiledOp) -> CompilerResult:
        op_type = op.op_type

        if op_type == "create_sketch":
            return self._compile_create_sketch(op)
        if op_type == "pad":
            return self._compile_pad(op)
        if op_type == "pocket":
            return self._compile_pocket(op)
        if op_type == "revolve":
            return self._compile_revolve(op)
        if op_type == "hole":
            return self._compile_hole(op)
        if op_type == "polar_pattern":
            return self._compile_polar_pattern(op)
        if op_type == "fillet":
            return self._compile_fillet(op)
        if op_type == "chamfer":
            return self._compile_chamfer(op)
        if op_type == "sweep":
            return self._compile_sweep(op)
        if op_type == "loft":
            return self._compile_loft(op)
        if op_type == "validate":
            return self._compile_validate(op)

        return CompilerResult(
            status=CompilerStatus.UNSUPPORTED,
            ops=None,
            notices=[
                Notice(
                    code="UNSUPPORTED_OP",
                    severity="error",
                    message=f"Unsupported operation type: {op_type}",
                    context={"operation_id": op.id},
                )
            ],
        )

    def _check_capability(self, op_name: str) -> CompilerStatus:
        if not self._capabilities:
            return CompilerStatus.UNSUPPORTED

        op_cap = self._capabilities.operations.get(op_name)
        if not op_cap:
            return CompilerStatus.UNSUPPORTED

        status = op_cap.status
        if status == "Yes":
            return CompilerStatus.COMPILED
        if status == "Partial":
            return CompilerStatus.LOWERED
        return CompilerStatus.UNSUPPORTED

    def _compile_create_sketch(self, op: CompiledOp) -> CompilerResult:
        cap_status = self._check_capability("create_sketch")

        if cap_status == CompilerStatus.UNSUPPORTED:
            return CompilerResult(
                status=CompilerStatus.UNSUPPORTED,
                ops=None,
                notices=[
                    Notice(
                        code="UNSUPPORTED_SKETCH",
                        severity="error",
                        message="Sketch creation not supported by FreeCAD backend",
                        context={"operation_id": op.id},
                    )
                ],
            )

        compiled = CompiledOp(
            id=op.id,
            op_type="cad.sketch",
            inputs=op.inputs,
            depends_on=op.depends_on,
            invariants=op.invariants,
            retry_policy=op.retry_policy,
            local_frame=op.local_frame,
            feature_provenance_id=op.feature_provenance_id,
            phase_id=op.phase_id,
            reference_support_type=op.reference_support_type,
            topology_sensitive=op.topology_sensitive,
        )

        return CompilerResult(
            status=CompilerStatus.COMPILED,
            ops=[compiled],
            notices=[],
        )

    def _compile_pad(self, op: CompiledOp) -> CompilerResult:
        cap_status = self._check_capability("pad")

        if cap_status == CompilerStatus.UNSUPPORTED:
            return CompilerResult(
                status=CompilerStatus.UNSUPPORTED,
                ops=None,
                notices=[
                    Notice(
                        code="UNSUPPORTED_PAD",
                        severity="error",
                        message="Pad operation not supported",
                        context={"operation_id": op.id},
                    )
                ],
            )

        compiled = CompiledOp(
            id=op.id,
            op_type="cad.pad",
            inputs=op.inputs,
            depends_on=op.depends_on,
            invariants=op.invariants,
            retry_policy=op.retry_policy,
            local_frame=op.local_frame,
            feature_provenance_id=op.feature_provenance_id,
            phase_id=op.phase_id,
            reference_support_type=op.reference_support_type,
            topology_sensitive=op.topology_sensitive,
        )

        return CompilerResult(
            status=CompilerStatus.COMPILED,
            ops=[compiled],
            notices=[],
        )

    def _compile_pocket(self, op: CompiledOp) -> CompilerResult:
        cap_status = self._check_capability("pocket")

        if cap_status == CompilerStatus.UNSUPPORTED:
            return CompilerResult(
                status=CompilerStatus.UNSUPPORTED,
                ops=None,
                notices=[
                    Notice(
                        code="UNSUPPORTED_POCKET",
                        severity="error",
                        message="Pocket operation not supported",
                        context={"operation_id": op.id},
                    )
                ],
            )

        compiled = CompiledOp(
            id=op.id,
            op_type="cad.pocket",
            inputs=op.inputs,
            depends_on=op.depends_on,
            invariants=op.invariants,
            retry_policy=op.retry_policy,
            local_frame=op.local_frame,
            feature_provenance_id=op.feature_provenance_id,
            phase_id=op.phase_id,
            reference_support_type=op.reference_support_type,
            topology_sensitive=op.topology_sensitive,
        )

        return CompilerResult(
            status=CompilerStatus.COMPILED,
            ops=[compiled],
            notices=[],
        )

    def _compile_revolve(self, op: CompiledOp) -> CompilerResult:
        cap_status = self._check_capability("revolve")

        if cap_status == CompilerStatus.UNSUPPORTED:
            return CompilerResult(
                status=CompilerStatus.UNSUPPORTED,
                ops=None,
                notices=[
                    Notice(
                        code="UNSUPPORTED_REVOLVE",
                        severity="error",
                        message="Revolve operation not supported",
                        context={"operation_id": op.id},
                    )
                ],
            )

        compiled = CompiledOp(
            id=op.id,
            op_type="cad.revolution",
            inputs=op.inputs,
            depends_on=op.depends_on,
            invariants=op.invariants,
            retry_policy=op.retry_policy,
            local_frame=op.local_frame,
            feature_provenance_id=op.feature_provenance_id,
            phase_id=op.phase_id,
            reference_support_type=op.reference_support_type,
            topology_sensitive=op.topology_sensitive,
        )

        return CompilerResult(
            status=CompilerStatus.COMPILED,
            ops=[compiled],
            notices=[],
        )

    def _compile_hole(self, op: CompiledOp) -> CompilerResult:
        cap_status = self._check_capability("hole")

        if cap_status == CompilerStatus.UNSUPPORTED:
            return CompilerResult(
                status=CompilerStatus.UNSUPPORTED,
                ops=None,
                notices=[
                    Notice(
                        code="UNSUPPORTED_HOLE",
                        severity="error",
                        message="Hole operation not supported",
                        context={"operation_id": op.id},
                    )
                ],
            )

        compiled = CompiledOp(
            id=op.id,
            op_type="cad.hole",
            inputs=op.inputs,
            depends_on=op.depends_on,
            invariants=op.invariants,
            retry_policy=op.retry_policy,
            local_frame=op.local_frame,
            feature_provenance_id=op.feature_provenance_id,
            phase_id=op.phase_id,
            reference_support_type=op.reference_support_type,
            topology_sensitive=op.topology_sensitive,
        )

        return CompilerResult(
            status=CompilerStatus.COMPILED,
            ops=[compiled],
            notices=[],
        )

    def _compile_polar_pattern(self, op: CompiledOp) -> CompilerResult:
        cap_status = self._check_capability("polar_pattern")

        if cap_status == CompilerStatus.UNSUPPORTED:
            return CompilerResult(
                status=CompilerStatus.UNSUPPORTED,
                ops=None,
                notices=[
                    Notice(
                        code="UNSUPPORTED_POLAR_PATTERN",
                        severity="error",
                        message="Polar pattern operation not supported",
                        context={"operation_id": op.id},
                    )
                ],
            )

        compiled = CompiledOp(
            id=op.id,
            op_type="cad.polar_pattern",
            inputs=op.inputs,
            depends_on=op.depends_on,
            invariants=op.invariants,
            retry_policy=op.retry_policy,
            local_frame=op.local_frame,
            feature_provenance_id=op.feature_provenance_id,
            phase_id=op.phase_id,
            reference_support_type=op.reference_support_type,
            topology_sensitive=op.topology_sensitive,
        )

        return CompilerResult(
            status=CompilerStatus.COMPILED,
            ops=[compiled],
            notices=[],
        )

    def _compile_fillet(self, op: CompiledOp) -> CompilerResult:
        cap_status = self._check_capability("fillet")

        if cap_status == CompilerStatus.UNSUPPORTED:
            return CompilerResult(
                status=CompilerStatus.UNSUPPORTED,
                ops=None,
                notices=[
                    Notice(
                        code="UNSUPPORTED_FILLET",
                        severity="error",
                        message="Fillet operation not supported",
                        context={"operation_id": op.id},
                    )
                ],
            )

        compiled = CompiledOp(
            id=op.id,
            op_type="cad.fillet",
            inputs=op.inputs,
            depends_on=op.depends_on,
            invariants=op.invariants,
            retry_policy=op.retry_policy,
            local_frame=op.local_frame,
            feature_provenance_id=op.feature_provenance_id,
            phase_id=op.phase_id,
            reference_support_type=op.reference_support_type,
            topology_sensitive=op.topology_sensitive,
        )

        return CompilerResult(
            status=CompilerStatus.COMPILED,
            ops=[compiled],
            notices=[],
        )

    def _compile_chamfer(self, op: CompiledOp) -> CompilerResult:
        cap_status = self._check_capability("chamfer")

        if cap_status == CompilerStatus.UNSUPPORTED:
            return CompilerResult(
                status=CompilerStatus.UNSUPPORTED,
                ops=None,
                notices=[
                    Notice(
                        code="UNSUPPORTED_CHAMFER",
                        severity="error",
                        message="Chamfer operation not supported",
                        context={"operation_id": op.id},
                    )
                ],
            )

        compiled = CompiledOp(
            id=op.id,
            op_type="cad.chamfer",
            inputs=op.inputs,
            depends_on=op.depends_on,
            invariants=op.invariants,
            retry_policy=op.retry_policy,
            local_frame=op.local_frame,
            feature_provenance_id=op.feature_provenance_id,
            phase_id=op.phase_id,
            reference_support_type=op.reference_support_type,
            topology_sensitive=op.topology_sensitive,
        )

        return CompilerResult(
            status=CompilerStatus.COMPILED,
            ops=[compiled],
            notices=[],
        )

    def _compile_sweep(self, op: CompiledOp) -> CompilerResult:
        subtractive = op.inputs.get("subtractive", False)
        cap_name = "sweep_subtractive" if subtractive else "sweep_additive"
        cap_status = self._check_capability(cap_name)

        if cap_status == CompilerStatus.UNSUPPORTED:
            return CompilerResult(
                status=CompilerStatus.UNSUPPORTED,
                ops=None,
                notices=[
                    Notice(
                        code="UNSUPPORTED_SWEEP",
                        severity="error",
                        message=f"Sweep operation ({cap_name}) not supported",
                        context={"operation_id": op.id},
                    )
                ],
            )

        compiled = CompiledOp(
            id=op.id,
            op_type="cad.sweep",
            inputs=op.inputs,
            depends_on=op.depends_on,
            invariants=op.invariants,
            retry_policy=op.retry_policy,
            local_frame=op.local_frame,
            feature_provenance_id=op.feature_provenance_id,
            phase_id=op.phase_id,
            reference_support_type=op.reference_support_type,
            topology_sensitive=op.topology_sensitive,
        )

        notices = []
        if cap_status == CompilerStatus.LOWERED:
            notices.append(
                Notice(
                    code="SWEEP_LOWERED",
                    severity="warning",
                    message="Sweep operation has partial support",
                    context={"operation_id": op.id},
                )
            )

        return CompilerResult(
            status=cap_status,
            ops=[compiled],
            notices=notices,
        )

    def _compile_loft(self, op: CompiledOp) -> CompilerResult:
        subtractive = op.inputs.get("subtractive", False)
        cap_name = "loft_subtractive" if subtractive else "loft_additive"
        cap_status = self._check_capability(cap_name)

        if cap_status == CompilerStatus.UNSUPPORTED:
            return CompilerResult(
                status=CompilerStatus.UNSUPPORTED,
                ops=None,
                notices=[
                    Notice(
                        code="UNSUPPORTED_LOFT",
                        severity="error",
                        message=f"Loft operation ({cap_name}) not supported",
                        context={"operation_id": op.id},
                    )
                ],
            )

        compiled = CompiledOp(
            id=op.id,
            op_type="cad.loft",
            inputs=op.inputs,
            depends_on=op.depends_on,
            invariants=op.invariants,
            retry_policy=op.retry_policy,
            local_frame=op.local_frame,
            feature_provenance_id=op.feature_provenance_id,
            phase_id=op.phase_id,
            reference_support_type=op.reference_support_type,
            topology_sensitive=op.topology_sensitive,
        )

        notices = []
        if cap_status == CompilerStatus.LOWERED:
            notices.append(
                Notice(
                    code="LOFT_LOWERED",
                    severity="warning",
                    message="Loft operation has partial support",
                    context={"operation_id": op.id},
                )
            )

        return CompilerResult(
            status=cap_status,
            ops=[compiled],
            notices=notices,
        )

    def _compile_validate(self, op: CompiledOp) -> CompilerResult:
        compiled = CompiledOp(
            id=op.id,
            op_type="cad.get_dimensions",
            inputs=op.inputs,
            depends_on=op.depends_on,
            invariants=op.invariants,
            retry_policy=op.retry_policy,
            local_frame=op.local_frame,
            feature_provenance_id=op.feature_provenance_id,
            phase_id=op.phase_id,
            reference_support_type=op.reference_support_type,
            topology_sensitive=op.topology_sensitive,
        )

        return CompilerResult(
            status=CompilerStatus.COMPILED,
            ops=[compiled],
            notices=[],
        )
