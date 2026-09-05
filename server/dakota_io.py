"""Dakota file protocol: params.in / results.out codecs and the fork bridge.

Dakota's mature integration path is file-driven: it writes a parameters file,
execs an "analysis driver", and reads a results file back. This module owns
that protocol so the real Dakota binary can be dropped in as a fast-follow —
``evaluator_fork`` is the analysis driver (also runnable as
``python -m server.dakota_io fork ...`` so a Dakota deck can name it
verbatim), and the codecs are proven against a fake Dakota double in tests
with zero binary dependency.

Format (the tolerant subset of Dakota's standard parameters format):

    <n> variables
    <value> <descriptor>          (n lines)
    <m> functions
    <asv> ASV_<i>:<label>         (m lines)
    <id> eval_id

Results: one ``<value> <tag>`` line per response, or the single token ``FAIL``.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from server.study_models import Study, StudyStatus, Variant


class DakotaFormatError(ValueError):
    pass


class DakotaEvalFailed(Exception):
    """The results file carries Dakota's FAIL token."""


def write_params_in(
    path: Path,
    params: dict[str, float],
    *,
    eval_id: int,
    response_labels: list[str] | None = None,
) -> None:
    labels = response_labels or []
    lines = [f"{len(params):>39} variables"]
    for name, value in params.items():
        lines.append(f"{value:>39.15e} {name}")
    lines.append(f"{len(labels):>39} functions")
    for i, label in enumerate(labels, start=1):
        lines.append(f"{1:>39} ASV_{i}:{label}")
    lines.append(f"{eval_id:>39} eval_id")
    path.write_text("\n".join(lines) + "\n")


def parse_params_in(path: Path) -> tuple[dict[str, float], int]:
    """Return ({descriptor: value}, eval_id)."""
    lines = [ln.strip() for ln in path.read_text().splitlines() if ln.strip()]
    params: dict[str, float] = {}
    eval_id = 0
    i = 0
    while i < len(lines):
        tokens = lines[i].split()
        if len(tokens) >= 2 and tokens[1] == "variables":
            try:
                count = int(tokens[0])
            except ValueError as e:
                raise DakotaFormatError(f"Bad variables count: {lines[i]!r}") from e
            for j in range(1, count + 1):
                if i + j >= len(lines):
                    raise DakotaFormatError("Truncated variables block")
                vtokens = lines[i + j].split()
                if len(vtokens) < 2:
                    raise DakotaFormatError(f"Bad variable line: {lines[i + j]!r}")
                try:
                    params[vtokens[1]] = float(vtokens[0])
                except ValueError as e:
                    raise DakotaFormatError(f"Bad variable value: {lines[i + j]!r}") from e
            i += count + 1
            continue
        if len(tokens) >= 2 and tokens[1] == "eval_id":
            try:
                eval_id = int(tokens[0])
            except ValueError as e:
                raise DakotaFormatError(f"Bad eval_id line: {lines[i]!r}") from e
        i += 1
    if not params:
        raise DakotaFormatError(f"No variables block found in {path}")
    return params, eval_id


def write_results_out(path: Path, values: list[tuple[float, str]]) -> None:
    path.write_text("".join(f"{value:.15e} {tag}\n" for value, tag in values))


def write_results_fail(path: Path) -> None:
    path.write_text("FAIL\n")


def parse_results_out(path: Path) -> list[tuple[float, str]]:
    """Parse a results file; raises DakotaEvalFailed on the FAIL token."""
    lines = [ln.strip() for ln in path.read_text().splitlines() if ln.strip()]
    if lines and lines[0].upper().startswith("FAIL"):
        raise DakotaEvalFailed(str(path))
    out: list[tuple[float, str]] = []
    for line in lines:
        tokens = line.split()
        try:
            value = float(tokens[0])
        except (IndexError, ValueError) as e:
            raise DakotaFormatError(f"Bad results line: {line!r}") from e
        tag = tokens[1] if len(tokens) > 1 else ""
        out.append((value, tag))
    return out


def evaluator_fork(
    params_in: Path,
    results_out: Path,
    *,
    revision: str,
    scenario: str,
    models: list[str],
    path_map: dict[str, str],
    response_metrics: list[str],
    seed: int = 0,
    artifacts_root: Path | None = None,
    workdir: Path | None = None,
) -> int:
    """The analysis driver Dakota execs: params.in → evaluator CLI → results.out.

    Returns the evaluator's exit code (0 on success, including cache replay).
    On any failure the results file carries the FAIL token so Dakota can move on.
    """
    try:
        params, eval_id = parse_params_in(params_in)
        bindings = [
            {"path": path_map[name], "value": value} for name, value in sorted(params.items())
        ]
    except (DakotaFormatError, KeyError) as exc:
        write_results_fail(results_out)
        print(f"error: {exc}", file=sys.stderr)
        return 1

    work = workdir if workdir is not None else results_out.parent / f"eval_{eval_id}"
    work.mkdir(parents=True, exist_ok=True)
    request_path = work / "request.json"
    result_path = work / "result.json"
    request_path.write_text(
        json.dumps(
            {
                "schema": "eval.request/v1",
                "revision": revision,
                "bindings": bindings,
                "scenario": scenario,
                "models": models,
                "seed": seed,
            },
            indent=2,
        )
    )

    argv = [sys.executable, "-m", "server.evaluator", str(request_path), str(result_path)]
    if artifacts_root is not None:
        argv += ["--artifacts-root", str(artifacts_root)]
    proc = subprocess.run(argv, capture_output=True, text=True)

    payload: dict[str, Any] | None = None
    if result_path.exists():
        try:
            payload = json.loads(result_path.read_text())
        except json.JSONDecodeError:
            payload = None

    if proc.returncode != 0 or payload is None or not payload.get("ok"):
        write_results_fail(results_out)
        return proc.returncode or 1

    metrics = payload.get("metrics", {})
    values: list[tuple[float, str]] = []
    for name in response_metrics:
        entry = metrics.get(name)
        if entry is None:
            write_results_fail(results_out)
            print(f"error: metric {name!r} missing from result", file=sys.stderr)
            return 1
        values.append((float(entry["value"]), name))
    write_results_out(results_out, values)
    return 0


def run_dakota_study(
    study: Study,
    backend: Any,
    *,
    dakota_argv: list[str],
    on_variant: Callable[[Variant | None], None],
    cancelled: Callable[[], bool],
) -> str | None:
    """Exec the Dakota binary over the fork bridge — fast-follow seam.

    The exec seam is established (a deck pointing at ``python -m
    server.dakota_io fork`` drives real evaluations); ranking Dakota's output
    back into study variants lands with the binary integration.
    """
    del backend, cancelled  # the binary drives evaluations via the fork bridge
    proc = subprocess.run(dakota_argv, capture_output=True, text=True)
    if proc.returncode != 0:
        study.status = StudyStatus.FAILED
        study.error = f"dakota exited {proc.returncode}: {proc.stderr.strip()[:500]}"
    else:
        study.status = StudyStatus.COMPLETE
    on_variant(None)
    return study.best_variant_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dakota analysis-driver bridge")
    sub = parser.add_subparsers(dest="command", required=True)
    fork = sub.add_parser("fork", help="params.in → evaluator → results.out")
    fork.add_argument("params_in", type=Path)
    fork.add_argument("results_out", type=Path)
    fork.add_argument("--revision", required=True)
    fork.add_argument("--scenario", default="latch_hold")
    fork.add_argument("--models", default="analytic_latch", help="comma-separated stage names")
    fork.add_argument(
        "--map",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="variable descriptor → binding path (repeatable)",
    )
    fork.add_argument("--metrics", default="latch_fos", help="comma-separated response metrics")
    fork.add_argument("--seed", type=int, default=0)
    fork.add_argument("--artifacts-root", type=Path, default=None)
    args = parser.parse_args(argv)

    path_map: dict[str, str] = {}
    for entry in args.map:
        name, sep, path = entry.partition("=")
        if not sep:
            print(f"error: bad --map entry {entry!r}", file=sys.stderr)
            return 2
        path_map[name] = path

    return evaluator_fork(
        args.params_in,
        args.results_out,
        revision=args.revision,
        scenario=args.scenario,
        models=[m for m in args.models.split(",") if m],
        path_map=path_map,
        response_metrics=[m for m in args.metrics.split(",") if m],
        seed=args.seed,
        artifacts_root=args.artifacts_root,
    )


if __name__ == "__main__":
    raise SystemExit(main())
