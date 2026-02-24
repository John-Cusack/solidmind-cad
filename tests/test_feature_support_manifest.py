from __future__ import annotations

import unittest
from pathlib import Path

from server.feature_support import evaluate_manifest, load_manifest, parse_matrix, summarize


REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "feature_support" / "manifest.yml"
MATRIX_PATH = REPO_ROOT / "FREECAD_CADQUERY_FEATURE_SUPPORT_MATRIX.md"


def _key(platform: str, feature: str) -> tuple[str, str]:
    return (platform.strip(), feature.strip())


class TestFeatureSupportManifest(unittest.TestCase):
    def test_manifest_exists_and_loads(self) -> None:
        self.assertTrue(MANIFEST_PATH.exists(), f"Missing manifest: {MANIFEST_PATH}")
        features = load_manifest(MANIFEST_PATH)
        self.assertGreater(len(features), 0)

    def test_manifest_ids_are_unique(self) -> None:
        features = load_manifest(MANIFEST_PATH)
        ids = [str(f["id"]) for f in features]
        self.assertEqual(len(ids), len(set(ids)), "Feature IDs must be unique")

    def test_manifest_rows_align_with_matrix_rows(self) -> None:
        features = load_manifest(MANIFEST_PATH)
        matrix_rows = parse_matrix(MATRIX_PATH)

        manifest_keys = {_key(str(f["platform"]), str(f["feature"])) for f in features}
        matrix_keys = {_key(row["platform"], row["feature"]) for row in matrix_rows}

        self.assertSetEqual(
            manifest_keys,
            matrix_keys,
            "Manifest and matrix must have identical platform/feature rows",
        )

    def test_baseline_status_and_usage_match_matrix(self) -> None:
        features = load_manifest(MANIFEST_PATH)
        matrix_rows = parse_matrix(MATRIX_PATH)

        matrix_index = {_key(row["platform"], row["feature"]): row for row in matrix_rows}

        for feature in features:
            key = _key(str(feature["platform"]), str(feature["feature"]))
            with self.subTest(platform=key[0], feature=key[1]):
                row = matrix_index[key]
                self.assertEqual(feature["baseline_status"], row["status"])
                self.assertEqual(feature["common_usage"], row["common_usage"])

    def test_computed_status_matches_baseline_snapshot(self) -> None:
        results = evaluate_manifest(MANIFEST_PATH)
        mismatches: list[str] = []
        for result in results:
            if result.computed_status != result.baseline_status:
                mismatches.append(
                    (
                        f"{result.platform} / {result.feature}: "
                        f"baseline={result.baseline_status}, computed={result.computed_status}, "
                        f"checks={result.passed_checks}/{result.total_checks}"
                    )
                )
        self.assertEqual([], mismatches, "\n".join(mismatches))

    def test_snapshot_counts_match_matrix(self) -> None:
        results = evaluate_manifest(MANIFEST_PATH)
        counts = summarize(results)
        self.assertEqual(15, counts["Yes"])
        self.assertEqual(0, counts["Partial"])
        self.assertEqual(4, counts["No"])


if __name__ == "__main__":
    unittest.main()
