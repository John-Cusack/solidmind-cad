"""Tests for orchestrator.dsm."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchestrator.dsm import (
    DSMEntry,
    build_matrix,
    cluster,
    export_artifact,
    load_artifact,
)


class TestBuildMatrix(unittest.TestCase):
    def test_symmetric(self) -> None:
        components = ["A", "B", "C"]
        entries = [DSMEntry("A", "B", "gear_mesh", 0.9)]
        dsm = build_matrix(components, entries)
        self.assertEqual(dsm.matrix[0][1], 0.9)
        self.assertEqual(dsm.matrix[1][0], 0.9)
        self.assertEqual(dsm.matrix[0][2], 0.0)

    def test_max_strength_wins(self) -> None:
        components = ["A", "B"]
        entries = [
            DSMEntry("A", "B", "gear_mesh", 0.5),
            DSMEntry("A", "B", "thermal", 0.8),
        ]
        dsm = build_matrix(components, entries)
        self.assertEqual(dsm.matrix[0][1], 0.8)

    def test_unknown_component_ignored(self) -> None:
        components = ["A", "B"]
        entries = [DSMEntry("A", "Z", "thermal", 0.5)]
        dsm = build_matrix(components, entries)
        self.assertEqual(dsm.matrix[0][1], 0.0)

    def test_empty(self) -> None:
        dsm = build_matrix([], [])
        self.assertEqual(dsm.matrix, [])


class TestCluster(unittest.TestCase):
    def test_single_cluster(self) -> None:
        components = ["A", "B", "C"]
        entries = [
            DSMEntry("A", "B", "mesh", 0.9),
            DSMEntry("B", "C", "mesh", 0.7),
        ]
        dsm = build_matrix(components, entries)
        clusters = cluster(dsm, threshold=0.5)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(sorted(clusters[0]), ["A", "B", "C"])

    def test_two_clusters(self) -> None:
        components = ["A", "B", "C", "D"]
        entries = [
            DSMEntry("A", "B", "mesh", 0.9),
            DSMEntry("C", "D", "mesh", 0.8),
        ]
        dsm = build_matrix(components, entries)
        clusters = cluster(dsm, threshold=0.5)
        self.assertEqual(len(clusters), 2)
        cluster_sets = [set(c) for c in clusters]
        self.assertIn({"A", "B"}, cluster_sets)
        self.assertIn({"C", "D"}, cluster_sets)

    def test_threshold_splits(self) -> None:
        components = ["A", "B", "C"]
        entries = [
            DSMEntry("A", "B", "mesh", 0.9),
            DSMEntry("B", "C", "thermal", 0.3),
        ]
        dsm = build_matrix(components, entries)
        clusters = cluster(dsm, threshold=0.5)
        self.assertEqual(len(clusters), 2)

    def test_singletons(self) -> None:
        components = ["A", "B", "C"]
        dsm = build_matrix(components, [])
        clusters = cluster(dsm, threshold=0.5)
        self.assertEqual(len(clusters), 3)


class TestJsonRoundTrip(unittest.TestCase):
    def test_export_and_load(self) -> None:
        components = ["A", "B", "C"]
        entries = [
            DSMEntry("A", "B", "gear_mesh", 0.9),
            DSMEntry("B", "C", "bolt", 0.6),
        ]
        dsm = build_matrix(components, entries)
        clusters_orig = cluster(dsm, threshold=0.5)

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = Path(f.name)
        try:
            export_artifact(dsm, clusters_orig, path)
            dsm_loaded, clusters_loaded = load_artifact(path)

            self.assertEqual(dsm_loaded.components, dsm.components)
            self.assertEqual(dsm_loaded.matrix, dsm.matrix)
            self.assertEqual(len(dsm_loaded.entries), len(dsm.entries))
            self.assertEqual(clusters_loaded, clusters_orig)
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
