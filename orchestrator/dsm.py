"""Design Structure Matrix — interaction analysis and clustering."""
from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class DSMEntry:
    """A pairwise interaction between two components."""

    component_a: str
    component_b: str
    interaction_type: str  # e.g. "gear_mesh", "bolt_pattern", "thermal"
    strength: float = 0.5  # 0.0–1.0


@dataclass(slots=True)
class DSMatrix:
    """A symmetric N×N design structure matrix."""

    components: list[str] = field(default_factory=list)
    entries: list[DSMEntry] = field(default_factory=list)
    matrix: list[list[float]] = field(default_factory=list)


def build_matrix(components: list[str], entries: list[DSMEntry]) -> DSMatrix:
    """Build a symmetric NxN matrix from components and pairwise entries."""
    n = len(components)
    idx = {name: i for i, name in enumerate(components)}
    matrix = [[0.0] * n for _ in range(n)]

    for entry in entries:
        a = idx.get(entry.component_a)
        b = idx.get(entry.component_b)
        if a is not None and b is not None:
            matrix[a][b] = max(matrix[a][b], entry.strength)
            matrix[b][a] = max(matrix[b][a], entry.strength)

    return DSMatrix(components=list(components), entries=list(entries), matrix=matrix)


def cluster(dsm: DSMatrix, threshold: float = 0.5) -> list[list[str]]:
    """Cluster components via BFS over edges above *threshold*.

    Returns a list of clusters (each a list of component names).
    Components with no edges above threshold form singleton clusters.
    """
    n = len(dsm.components)
    visited = [False] * n

    # Build adjacency from matrix
    adj: dict[int, list[int]] = {i: [] for i in range(n)}
    for i in range(n):
        for j in range(i + 1, n):
            if dsm.matrix[i][j] >= threshold:
                adj[i].append(j)
                adj[j].append(i)

    clusters: list[list[str]] = []
    for start in range(n):
        if visited[start]:
            continue
        group: list[str] = []
        queue: deque[int] = deque([start])
        visited[start] = True
        while queue:
            node = queue.popleft()
            group.append(dsm.components[node])
            for nb in adj[node]:
                if not visited[nb]:
                    visited[nb] = True
                    queue.append(nb)
        clusters.append(group)

    return clusters


def export_artifact(dsm: DSMatrix, clusters: list[list[str]], path: str | Path) -> None:
    """Save DSM and clusters to JSON."""
    data: dict[str, Any] = {
        "components": dsm.components,
        "entries": [
            {
                "component_a": e.component_a,
                "component_b": e.component_b,
                "interaction_type": e.interaction_type,
                "strength": e.strength,
            }
            for e in dsm.entries
        ],
        "matrix": dsm.matrix,
        "clusters": clusters,
    }
    Path(path).write_text(json.dumps(data, indent=2))


def load_artifact(path: str | Path) -> tuple[DSMatrix, list[list[str]]]:
    """Load DSM and clusters from JSON."""
    data = json.loads(Path(path).read_text())
    entries = [
        DSMEntry(
            component_a=e["component_a"],
            component_b=e["component_b"],
            interaction_type=e["interaction_type"],
            strength=e.get("strength", 0.5),
        )
        for e in data.get("entries", [])
    ]
    dsm = DSMatrix(
        components=data.get("components", []),
        entries=entries,
        matrix=data.get("matrix", []),
    )
    clusters = data.get("clusters", [])
    return dsm, clusters
