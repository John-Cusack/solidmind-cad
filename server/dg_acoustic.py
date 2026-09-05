"""Design-graph builder for a distributed acoustic detection mesh.

The second domain. Unlike the foam-dart latch (imported from a committed
``design.brief/v1`` fixture), this revision is constructed programmatically —
briefs describe mechanical assemblies, and a sensor mesh is mostly placements
and environment.

**Node placements live in the design graph**, not the scenario: placement is
the design variable of a detection mesh, and filing it in the world would
break attribution for the flagship study. Atmospheric state stays in the
params doc under ``/environment`` and is *selected* by scenario name, matching
the tranche-1 shape where a scenario is a named selector rather than its own
artifact.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from server.dg_models import (
    PARAMS_SCHEMA,
    Component,
    ParamSpec,
    StructureDoc,
    ValidityDomain,
)
from server.dg_store import commit_revision

LITERATURE = "literature:acoustic-array-defaults"


def build_mesh(
    *,
    node_positions: dict[str, tuple[float, float]] | None = None,
    aperture_m: float = 0.5,
    integration_time_s: float = 0.5,
    target_xy: tuple[float, float] = (0.0, 300.0),
    f_lo_hz: float = 100.0,
    f_hi_hz: float = 400.0,
    snr_db: float = 0.0,
    position_requirement_m: float = 25.0,
    name: str = "Acoustic Detection Mesh",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (structure_doc, params_doc) for an N-node bearing mesh."""
    nodes = node_positions or {"node_a": (-100.0, 0.0), "node_b": (100.0, 0.0)}
    if len(nodes) < 2:
        raise ValueError("a bearing mesh needs at least two nodes")

    components = [
        Component(id=nid, kind="sensor_node", quantity=1, ports=("array",)) for nid in sorted(nodes)
    ]
    specs: list[ParamSpec] = []
    params: dict[str, Any] = {"schema": PARAMS_SCHEMA, "nodes": {}}

    def add(path: str, value: Any, unit: str, domain: ValidityDomain | None = None) -> None:
        specs.append(
            ParamSpec(path=path, unit=unit, kind="float", domain=domain, provenance=LITERATURE)
        )
        cursor = params
        tokens = path.strip("/").split("/")
        for token in tokens[:-1]:
            cursor = cursor.setdefault(token, {})
        cursor[tokens[-1]] = {"value": float(value), "unit": unit}

    for nid in sorted(nodes):
        x, y = nodes[nid]
        add(f"/nodes/{nid}/x_m", x, "m")
        add(f"/nodes/{nid}/y_m", y, "m")

    # Aperture domain: the coherence model is a literature parameterization
    # whose 5/3-law form is only meaningful within the inertial subrange over
    # realistic array spans. Beyond it the model is extrapolating.
    add(
        "/array/aperture_m",
        aperture_m,
        "m",
        ValidityDomain(
            min_value=0.05,
            max_value=6.0,
            note="5/3-law transverse coherence; literature parameters, uncalibrated",
        ),
    )
    add(
        "/array/integration_time_s",
        integration_time_s,
        "s",
        ValidityDomain(
            min_value=0.01,
            max_value=1.0,
            note="turbulence coherence time caps useful integration",
        ),
    )
    add("/target/x_m", target_xy[0], "m")
    add("/target/y_m", target_xy[1], "m")
    add("/source/f_lo_hz", f_lo_hz, "Hz")
    add("/source/f_hi_hz", f_hi_hz, "Hz")
    add("/source/snr_db", snr_db, "dB")
    # Acoustic index-of-refraction structure parameter. The convective value
    # puts the transverse coherence length at a few metres over a few hundred
    # metres of path, consistent with the assumptions register's 1-10 m band
    # (medium confidence — the measurement campaign is what settles it).
    add("/environment/cn2_calm", 1e-8, "m^-2/3")
    add("/environment/cn2_convective", 1e-5, "m^-2/3")
    add("/constraints/position_requirement_m", position_requirement_m, "m")

    structure = StructureDoc(
        name=name,
        components=tuple(components),
        interfaces=(),
        frames={"origin": {"x_m": 0.0, "y_m": 0.0}},
        materials={},
        param_specs=tuple(specs),
    )
    return structure.to_dict(), params


def commit_mesh(*, root: Path | None = None, parent: str | None = None, **kwargs: Any) -> str:
    """Build and commit a mesh revision; returns the revision id."""
    structure, params = build_mesh(**kwargs)
    return commit_revision(
        structure, params, parent=parent, meta={"builder": "dg_acoustic.build_mesh"}, root=root
    )
