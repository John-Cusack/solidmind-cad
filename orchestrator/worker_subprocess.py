"""MVP worker execution — launches `claude --print` subprocesses.

The orchestrator is a standalone Python process (NOT run from inside
Claude Code). Each worker is an independent `claude --print` session,
so there is no nested-session restriction. Max plan users pay nothing
extra for worker sessions.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestrator.spec import (
    ComplexityClass,
    Interface,
    MasterSpec,
    RuntimePolicy,
    Subsystem,
    WorkerResult,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

_WORKER_PROMPT_TEMPLATE = """\
You are a CAD worker building **{part_name}** for the **{assembly_name}** assembly.
Other workers are building mating parts to the same interface specs.

## Your Assignment
{description}

## Specifications
{specs_yaml}

## Material
{material}

## Envelope Constraint
Your part must fit within: {envelope_mm} mm bounding box.

## Mass Budget
Maximum: {mass_budget_kg} kg

## Manufacturing Constraints
Process: {mfg_process}
Min feature size: {mfg_min_feature} mm
Min wall thickness: {mfg_min_wall} mm

## Interfaces (FROZEN — match these exactly)
{interfaces_text}

These interface dimensions are contractual. Your mating parts depend on them.
If you cannot meet an interface spec, report it in metadata — do NOT deviate silently.

{skeleton_section}## Deliverables
1. Build the part using cad.* tools
2. Export: cad_export(path="{output_dir}/{part_name}.step", format="step")
3. Export: cad_export(path="{output_dir}/{part_name}.stl", format="stl")
4. Take a screenshot: cad_screenshot(path="{output_dir}/{part_name}.png")
5. Measure each interface dimension using cad_measure_between or cad_get_dimensions
6. Write {output_dir}/metadata.json with:
   {{
     "subsystem": "{part_name}",
     "claimed_mass_kg": <measured>,
     "claimed_bounding_box_mm": [x, y, z],
     "interface_actuals": {{
       "<interface_id>": {{
         "<check_point_feature>": <measured_value_mm>
       }}
     }},
     "screenshots": ["{part_name}.png"],
     "deviations": ["any interface specs you could not meet"],
     "notes": "free text"
   }}
"""


def build_worker_prompt(
    spec: MasterSpec,
    subsystem: Subsystem,
    interfaces: list[Interface],
    output_dir: str,
) -> str:
    """Construct the prompt for a worker's Claude Code session."""
    import yaml

    specs_yaml = yaml.dump(subsystem.specs, default_flow_style=False) if subsystem.specs else "(none)"

    ifc_parts = []
    for ifc in interfaces:
        ifc_parts.append(f"### {ifc.name} (id: {ifc.id})")
        if ifc.mating.type:
            ifc_parts.append(f"Type: {ifc.mating.type}")
        if ifc.geometry:
            ifc_parts.append(f"Geometry: {json.dumps(ifc.geometry)}")
        if ifc.tolerances.fit_class:
            ifc_parts.append(f"Tolerances: fit={ifc.tolerances.fit_class}")
        if ifc.datum_scheme:
            ifc_parts.append(f"Datum scheme: {ifc.datum_scheme}")
        ifc_parts.append("")

    skeleton_section = _build_skeleton_section(spec, subsystem)

    return _WORKER_PROMPT_TEMPLATE.format(
        part_name=subsystem.name,
        assembly_name=spec.name,
        description=subsystem.description,
        specs_yaml=specs_yaml,
        material=subsystem.material or "(not specified)",
        envelope_mm=subsystem.envelope_mm or "(unconstrained)",
        mass_budget_kg=subsystem.mass_budget_kg or "(unconstrained)",
        mfg_process=subsystem.manufacturing.process or "(any)",
        mfg_min_feature=subsystem.manufacturing.min_feature_size_mm,
        mfg_min_wall=subsystem.manufacturing.min_wall_mm,
        interfaces_text="\n".join(ifc_parts) if ifc_parts else "(none)",
        output_dir=output_dir,
        skeleton_section=skeleton_section,
    )


def _build_skeleton_section(spec: MasterSpec, subsystem: Subsystem) -> str:
    """Build a ## Spatial Constraints section from skeleton data."""
    sk = spec.skeleton
    lines: list[str] = []

    # Reserved volume for this subsystem
    reserved = sk.reserved_volumes.get(subsystem.name)
    if reserved:
        lines.append(f"Reserved volume: {json.dumps(reserved)}")

    # Relevant datums
    ac = subsystem.assembly_constraints
    datum_refs = set()
    for key in ("datum", "datums", "coaxial_with", "mounted_on"):
        val = ac.get(key)
        if val:
            if isinstance(val, list):
                datum_refs.update(val)
            else:
                datum_refs.add(val)
    if subsystem.name in sk.datums:
        datum_refs.add(subsystem.name)
    if datum_refs:
        relevant_datums = {
            k: v for k, v in sk.datums.items() if k in datum_refs
        }
        if relevant_datums:
            lines.append(f"Datums: {json.dumps(relevant_datums)}")

    # Keepout zones that overlap this subsystem's reserved volume
    if reserved and sk.keepout_zones:
        from orchestrator.skeleton import aabb_overlap
        overlapping = []
        for ki, kz in enumerate(sk.keepout_zones):
            if aabb_overlap(reserved, kz):
                overlapping.append(kz.get("name", f"keepout_{ki}"))
        if overlapping:
            lines.append(f"Nearby keepout zones (avoid): {overlapping}")

    if not lines:
        return ""
    return "## Spatial Constraints\n" + "\n".join(lines) + "\n\n"


# ---------------------------------------------------------------------------
# Worker result
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SubprocessResult:
    """Raw result from a `claude --print` subprocess."""

    subsystem_name: str
    worker_index: int
    returncode: int
    stdout: str
    stderr: str
    elapsed_sec: float
    output_dir: Path
    timed_out: bool = False


# ---------------------------------------------------------------------------
# Single worker launch
# ---------------------------------------------------------------------------


async def launch_worker(
    spec: MasterSpec,
    subsystem: Subsystem,
    interfaces: list[Interface],
    *,
    run_dir: Path,
    worker_index: int = 0,
    claude_binary: str = "claude",
) -> SubprocessResult:
    """Launch a single `claude --print` subprocess for one subsystem variant.

    Args:
        spec: The master spec (for context).
        subsystem: The subsystem to build.
        interfaces: Frozen interfaces relevant to this subsystem.
        run_dir: Base directory for this orchestrator run.
        worker_index: Variant index (0-based).
        claude_binary: Path to the claude CLI binary.
    """
    worker_dir = run_dir / f"{subsystem.name}_{worker_index}"
    output_dir = worker_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    # Write sub-spec for provenance
    input_path = worker_dir / "input.yaml"
    _write_sub_spec(input_path, spec, subsystem, interfaces)

    prompt = build_worker_prompt(spec, subsystem, interfaces, str(output_dir))
    policy = subsystem.effective_runtime_policy()

    log.info(
        "Launching worker %s_%d (timeout=%ds)",
        subsystem.name, worker_index, policy.timeout_sec,
    )

    start = time.monotonic()
    timed_out = False
    try:
        proc = await asyncio.create_subprocess_exec(
            claude_binary,
            "--print",
            "-p", prompt,
            "--allowedTools", "mcp__solidmind-cad__*",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(worker_dir),
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(),
            timeout=policy.timeout_sec,
        )
        returncode = proc.returncode or 0
    except asyncio.TimeoutError:
        log.warning("Worker %s_%d timed out after %ds", subsystem.name, worker_index, policy.timeout_sec)
        proc.kill()
        stdout_bytes, stderr_bytes = await proc.communicate()
        returncode = -1
        timed_out = True

    elapsed = time.monotonic() - start
    return SubprocessResult(
        subsystem_name=subsystem.name,
        worker_index=worker_index,
        returncode=returncode,
        stdout=stdout_bytes.decode("utf-8", errors="replace"),
        stderr=stderr_bytes.decode("utf-8", errors="replace"),
        elapsed_sec=elapsed,
        output_dir=output_dir,
        timed_out=timed_out,
    )


# ---------------------------------------------------------------------------
# Batch dispatch
# ---------------------------------------------------------------------------


async def dispatch_workers(
    spec: MasterSpec,
    *,
    run_dir: Path,
    claude_binary: str = "claude",
    max_parallel: int = 4,
) -> list[SubprocessResult]:
    """Launch all workers for generated subsystems, with concurrency limit.

    Only subsystems with kind=GENERATED are dispatched. Catalog and standard
    parts are not built by workers.
    """
    from orchestrator.spec import SubsystemKind

    tasks: list[tuple[Subsystem, list[Interface], int]] = []
    for sub in spec.subsystems:
        if sub.kind != SubsystemKind.GENERATED:
            continue
        interfaces = spec.interfaces_for(sub.name)
        for i in range(sub.worker_count):
            tasks.append((sub, interfaces, i))

    log.info("Dispatching %d worker(s) (max_parallel=%d)", len(tasks), max_parallel)

    sem = asyncio.Semaphore(max_parallel)
    results: list[SubprocessResult] = []

    async def _run(sub: Subsystem, ifcs: list[Interface], idx: int) -> SubprocessResult:
        async with sem:
            return await launch_worker(
                spec, sub, ifcs,
                run_dir=run_dir,
                worker_index=idx,
                claude_binary=claude_binary,
            )

    coros = [_run(sub, ifcs, idx) for sub, ifcs, idx in tasks]
    results = await asyncio.gather(*coros)
    return list(results)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_sub_spec(
    path: Path,
    spec: MasterSpec,
    subsystem: Subsystem,
    interfaces: list[Interface],
) -> None:
    """Write a sub-spec YAML file for provenance."""
    import yaml

    data = {
        "master_spec_id": spec.id,
        "master_spec_name": spec.name,
        "subsystem": MasterSpec._sub_to_dict(subsystem),
        "interfaces": [MasterSpec._ifc_to_dict(i) for i in interfaces],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))


def find_claude_binary() -> str:
    """Find the `claude` CLI binary on PATH."""
    path = shutil.which("claude")
    if not path:
        raise FileNotFoundError(
            "Claude Code CLI not found on PATH. Install with: "
            "npm install -g @anthropic-ai/claude-code"
        )
    return path
