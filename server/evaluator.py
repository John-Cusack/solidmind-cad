"""Evaluator CLI — ``evaluate(revision, params, scenario, models, seed) → result``.

File-driven and Dakota-shaped: read a request JSON, run the tiered stages,
write a result JSON. Everything consumed and produced is content-addressed;
the request hash (which includes environment identity and per-stage model
identities) is the evaluation cache key, so an identical request replays the
stored result byte-for-byte instead of recomputing.

    python -m server.evaluator <request.json> <result.json> [--artifacts-root PATH]

Exit codes:
    0  ok (including cache replay)
    1  internal error
    2  binding hard-fail (layer 1; result file still written, machine-readable)
    3  invalid request / unknown revision / unknown model
    4  model (stage) error

The layered binding check: layer 1 violations abort before materialization
(exit 2). Layer 2 — bindings applied but the materialized fingerprint is
unchanged — sets ``flags.unchanged_fingerprint`` on an ok result; it is a
zero-sensitivity signal, never a failure.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from server import artifact_store as cas
from server import jcs
from server.dg_binding import BindingError, apply_bindings, domain_hits, validate_bindings
from server.dg_models import hash_doc
from server.dg_store import load_revision
from server.env_identity import environment_identity, environment_identity_hash
from server.eval_models import EvalRequest, EvalResult, FailureClass
from server.eval_stages import STAGES, Stage, StageError, resolve_stages
from server.model_registry import ModelRegistryError, load_chain, uncalibrated_terms

MATERIALIZED_SCHEMA = "eval.materialized/v1"
RESULT_NAMESPACE = "eval_results"

EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_BINDING = 2
EXIT_INVALID = 3
EXIT_MODEL = 4

_FAILURE_EXITS = {
    FailureClass.BINDING_ERROR: EXIT_BINDING,
    FailureClass.REVISION_NOT_FOUND: EXIT_INVALID,
    FailureClass.INVALID_REQUEST: EXIT_INVALID,
    FailureClass.MODEL_ERROR: EXIT_MODEL,
    FailureClass.INTERNAL: EXIT_INTERNAL,
}


def _failure_result(
    request: EvalRequest | None, failure_class: FailureClass, message: str
) -> EvalResult:
    return EvalResult(
        ok=False,
        revision=request.revision if request is not None else "",
        request_hash="",
        seed=request.seed if request is not None else 0,
        environment={
            **environment_identity(),
            "identity_hash": environment_identity_hash(),
        },
        failure={"class": failure_class.value, "message": message},
    )


def _materialize(
    structure: dict[str, Any],
    params: dict[str, Any],
    scenario: str,
    stages: dict[str, Stage],
) -> dict[str, Any]:
    """The fingerprinted artifact: every requested stage's derived inputs."""
    derived: dict[str, Any] = {
        name: stage.derive_inputs(structure, params, scenario) for name, stage in stages.items()
    }
    return {"schema": MATERIALIZED_SCHEMA, "scenario": scenario, "stages": derived}


def evaluate_request(
    request: EvalRequest, *, root: Path | None = None
) -> tuple[EvalResult, int, bool]:
    """Run one evaluation. Returns (result, exit_code, was_cache_hit)."""
    # Resolve revision and models.
    try:
        resolved = cas.resolve(request.revision, root=root)
    except cas.ArtifactError as e:
        return (
            _failure_result(request, FailureClass.REVISION_NOT_FOUND, str(e)),
            EXIT_INVALID,
            False,
        )

    unknown = [m for m in request.models if m not in STAGES]
    if unknown:
        return (
            _failure_result(request, FailureClass.INVALID_REQUEST, f"Unknown model(s): {unknown}"),
            EXIT_INVALID,
            False,
        )
    # Model chain: which terms are enabled per stage. An ablated chain changes
    # each stage's identity, and identity is part of the cache key.
    chain = None
    if request.models_revision is not None:
        try:
            chain = load_chain(request.models_revision, root=root)
        except ModelRegistryError as e:
            return (
                _failure_result(request, FailureClass.INVALID_REQUEST, str(e)),
                EXIT_INVALID,
                False,
            )
    try:
        stages = resolve_stages(request.models, chain)
    except StageError as e:
        return (
            _failure_result(request, FailureClass.INVALID_REQUEST, str(e)),
            EXIT_INVALID,
            False,
        )
    model_identity = {name: stage.identity for name, stage in stages.items()}

    request_hash = request.request_hash(
        resolved_revision=resolved,
        env_identity_hash=environment_identity_hash(),
        model_identities=model_identity,
    )

    # Cache replay: sound because the key includes environment + model identity
    # and the analytic tier is bit-exact.
    cached = cas.get_ref(RESULT_NAMESPACE, request_hash, root=root)
    if cached is not None:
        result = EvalResult.from_dict(cas.get_json(cached["hash"], root=root))
        return result, EXIT_OK, True

    try:
        structure, params, _manifest = load_revision(resolved, root=root)
    except (cas.ArtifactError, ValueError) as e:
        return (
            _failure_result(request, FailureClass.REVISION_NOT_FOUND, str(e)),
            EXIT_INVALID,
            False,
        )

    # Layer 1: hard fail before materialization.
    try:
        validate_bindings(structure, params, request.bindings)
    except BindingError as e:
        result = _failure_result(request, FailureClass.BINDING_ERROR, f"{e.code}: {e}")
        result.revision = resolved
        return result, EXIT_BINDING, False

    bound_params = apply_bindings(params, request.bindings)

    try:
        materialized = _materialize(structure, bound_params, request.scenario, stages)
        base_hash = (
            hash_doc(_materialize(structure, params, request.scenario, stages))
            if request.bindings
            else None
        )
    except StageError as e:
        result = _failure_result(request, FailureClass.MODEL_ERROR, str(e))
        result.revision = resolved
        return result, EXIT_MODEL, False

    bound_params_hash = cas.put_json(bound_params, root=root)
    materialized_hash = cas.put_json(materialized, root=root)
    flags: dict[str, Any] = {
        "unchanged_fingerprint": base_hash is not None and base_hash == materialized_hash
    }

    # Run the stages on the materialized inputs.
    metrics: dict[str, Any] = {}
    checks: list[dict[str, Any]] = []
    try:
        for name, stage in stages.items():
            output = stage.run(materialized["stages"][name], request.seed)
            for key, value in output.metrics.items():
                metrics[key if key not in metrics else f"{name}.{key}"] = value
            checks.extend(output.checks)
    except StageError as e:
        result = _failure_result(request, FailureClass.MODEL_ERROR, str(e))
        result.revision = resolved
        return result, EXIT_MODEL, False

    structure_hash = hash_doc(structure)
    params_hash = hash_doc(params)

    result = EvalResult(
        ok=True,
        revision=resolved,
        request_hash=request_hash,
        metrics=metrics,
        checks=checks,
        # Terms enabled without an anchored validity envelope. Screening-grade
        # physics is usable; an optimum that leans on it is a calibration debt.
        uncalibrated_models=[
            f"{t.stage}:{t.id}"
            for t in uncalibrated_terms(chain)
            if t.stage in stages  # only the stages this evaluation actually ran
        ]
        if chain is not None
        else [],
        applied_bindings=[b.to_dict() for b in request.bindings],
        artifact_hashes={
            "structure": structure_hash,
            "params": params_hash,
            "bound_params": bound_params_hash,
            "materialized": materialized_hash,
        },
        seed=request.seed,
        environment={
            **environment_identity(),
            "identity_hash": environment_identity_hash(),
        },
        model_identity=model_identity,
        validity_domain_hits=domain_hits(structure, bound_params),
        flags=flags,
    )

    result_hash = cas.put_json(result.to_dict(), root=root)
    cas.record_lineage(
        result_hash,
        op="evaluate",
        inputs={
            "revision": resolved,
            "bound_params": bound_params_hash,
            "materialized": materialized_hash,
        },
        env_identity_hash=environment_identity_hash(),
        root=root,
    )
    cas.set_ref(RESULT_NAMESPACE, request_hash, result_hash, root=root)
    return result, EXIT_OK, False


def _write_result_file(result: EvalResult, path: Path) -> None:
    # Round-trip through JCS so the display file is written from the canonical
    # domain (e.g. 60.0 and 60 are the same JCS number) — a live result and a
    # cache-replayed result must produce byte-identical files.
    normalized = json.loads(jcs.canonicalize(result.to_dict()))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    tmp.write_text(json.dumps(normalized, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one design-graph evaluation")
    parser.add_argument("request", type=Path)
    parser.add_argument("result", type=Path)
    parser.add_argument("--artifacts-root", type=Path, default=None)
    args = parser.parse_args(argv)

    request: EvalRequest | None = None
    try:
        request = EvalRequest.from_dict(json.loads(args.request.read_text()))
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        result = _failure_result(request, FailureClass.INVALID_REQUEST, f"Bad request: {e}")
        _write_result_file(result, args.result)
        print(f"error: {e}", file=sys.stderr)
        return EXIT_INVALID

    try:
        result, exit_code, _cache_hit = evaluate_request(request, root=args.artifacts_root)
    except Exception as e:  # noqa: BLE001 — CLI boundary; report as INTERNAL
        result = _failure_result(request, FailureClass.INTERNAL, f"{type(e).__name__}: {e}")
        _write_result_file(result, args.result)
        print(f"error: {e}", file=sys.stderr)
        return EXIT_INTERNAL

    _write_result_file(result, args.result)
    if result.ok:
        result_hash = hash_doc(result.to_dict())
        print(f"result_sha256={result_hash}")
    else:
        assert result.failure is not None
        print(f"error: {result.failure['class']}: {result.failure['message']}", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
