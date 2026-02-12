from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure repo root is on sys.path when run as a script.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from server.feature_support import (  # noqa: E402
    FeatureResult,
    evaluate_manifest,
    parse_matrix,
    summarize,
)


def _row_key(platform: str, feature: str) -> tuple[str, str]:
    return (platform.strip(), feature.strip())


def _print_feature_table(results: list[FeatureResult]) -> None:
    print("Feature Support Report")
    print("======================")
    for result in results:
        print(
            f"[{result.computed_status:7}] {result.platform:8} | "
            f"{result.feature} ({result.passed_checks}/{result.total_checks})"
        )


def _print_summary(results: list[FeatureResult]) -> None:
    counts = summarize(results)
    total = len(results)
    print("")
    print("Summary")
    print("-------")
    print(f"Total:   {total}")
    print(f"Yes:     {counts['Yes']}")
    print(f"Partial: {counts['Partial']}")
    print(f"No:      {counts['No']}")


def _matrix_alignment(
    results: list[FeatureResult],
    matrix_path: Path,
) -> tuple[int, int, int, list[str]]:
    matrix_rows = parse_matrix(matrix_path)

    matrix_index = {
        _row_key(row["platform"], row["feature"]): row
        for row in matrix_rows
    }
    result_index = {
        _row_key(result.platform, result.feature): result
        for result in results
    }

    problems: list[str] = []

    missing_in_manifest = sorted(set(matrix_index.keys()) - set(result_index.keys()))
    extra_in_manifest = sorted(set(result_index.keys()) - set(matrix_index.keys()))
    status_mismatches = 0

    for key in sorted(set(matrix_index.keys()) & set(result_index.keys())):
        row = matrix_index[key]
        result = result_index[key]
        if row["status"] != result.baseline_status:
            status_mismatches += 1
            problems.append(
                "Baseline mismatch for "
                f"{result.platform} / {result.feature}: "
                f"matrix={row['status']} manifest={result.baseline_status}"
            )
        if row["common_usage"] != result.common_usage:
            problems.append(
                "Common-usage mismatch for "
                f"{result.platform} / {result.feature}: "
                f"matrix={row['common_usage']} manifest={result.common_usage}"
            )

    for platform, feature in missing_in_manifest:
        problems.append(f"Missing in manifest: {platform} / {feature}")
    for platform, feature in extra_in_manifest:
        problems.append(f"Extra in manifest: {platform} / {feature}")

    return (
        len(missing_in_manifest),
        len(extra_in_manifest),
        status_mismatches,
        problems,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate feature support from feature_support/manifest.yml and optionally "
            "check alignment with FREECAD_CADQUERY_FEATURE_SUPPORT_MATRIX.md"
        )
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("feature_support/manifest.yml"),
        help="Path to feature support manifest YAML",
    )
    parser.add_argument(
        "--matrix",
        type=Path,
        default=None,
        help="Path to feature matrix markdown for alignment checks",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Return non-zero unless all computed statuses are Yes and matrix alignment is clean.",
    )
    args = parser.parse_args(argv)

    results = evaluate_manifest(args.manifest)
    _print_feature_table(results)
    _print_summary(results)

    alignment_problems: list[str] = []
    if args.matrix is not None:
        missing, extra, mismatches, alignment_problems = _matrix_alignment(results, args.matrix)
        print("")
        print("Matrix Alignment")
        print("----------------")
        print(f"Missing in manifest: {missing}")
        print(f"Extra in manifest:   {extra}")
        print(f"Status mismatches:   {mismatches}")
        if alignment_problems:
            print("Problems:")
            for item in alignment_problems:
                print(f"- {item}")

    counts = summarize(results)
    has_non_yes = (counts["Partial"] + counts["No"]) > 0
    has_alignment_errors = len(alignment_problems) > 0

    if args.strict:
        return 1 if has_non_yes or has_alignment_errors else 0

    return 1 if has_alignment_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
