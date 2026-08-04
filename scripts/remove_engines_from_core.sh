#!/usr/bin/env bash
# Remove the engine packages from core — the last, deliberate half of the split.
#
# DO NOT RUN THIS until the sibling repositories are pushed somewhere durable.
# Until then core's copies are the only ones that exist off this machine, and
# this script is the step that makes them the only ones anywhere.
#
# What it removes:
#   isaac_bridge/  gazebo_bridge/  chrono_daemon/  chrono_bridge/  rl_training/
#   plus the tests in core that belong to them
#
# What core keeps (docs/engine-integration-architecture.md §4):
#   the contract (docs/engine-contract.md + schemas/), engines.d/ descriptors,
#   the registry, one generic client, the reference engine, the TCK,
#   the model pipeline, the tool façades and orchestration
#
# Usage:
#   scripts/remove_engines_from_core.sh --dry-run     # default: show, change nothing
#   scripts/remove_engines_from_core.sh --confirm     # actually remove

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIRM=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --confirm) CONFIRM=1; shift ;;
        --dry-run) CONFIRM=0; shift ;;
        -h|--help) sed -n '2,20p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; exit 2 ;;
    esac
done

cd "${REPO_ROOT}"

PACKAGES=(isaac_bridge gazebo_bridge chrono_daemon chrono_bridge rl_training)
TEST_GLOBS=(
    "tests/test_isaac_*.py"
    "tests/test_teleop_lifecycle.py"
    "tests/test_gazebo_*.py"
    "tests/test_package_to_sdf.py"
    "tests/test_px4_airframe.py"
    "tests/test_mavlink_controller.py"
    "tests/test_chrono_*.py"
    "tests/test_urdf_analyzer.py"
)

echo "Packages:"
for package in "${PACKAGES[@]}"; do
    [[ -d "${package}" ]] && echo "  ${package}  ($(git ls-files "${package}" | wc -l) tracked files)"
done

echo "Tests:"
for glob in "${TEST_GLOBS[@]}"; do
    for path in ${glob}; do
        [[ -e "${path}" ]] && echo "  ${path}"
    done
done

cat <<'NOTE'

Also review by hand — these span both sides and may belong in core:
  tests/test_freecad_to_gazebo.py    core's export pipeline, driven end to end
  tests/test_full_pipeline_e2e.py    core + chrono + FreeCAD
  tests/test_sim_real_backends.py    core's client against real engines
  examples/quadrotor_camera_drone/   imports gazebo_bridge directly

And after removal:
  - server/tools_rl.py imports rl_training lazily; point it at the CLI
    (step 7) or the installed solidmind-rl package
  - server/study_solvers.py's chrono solver drives the engine over the
    contract already, so it keeps working
  - tests/test_import_boundaries.py should drop the packages it no longer sees
NOTE

if [[ "${CONFIRM}" -ne 1 ]]; then
    echo
    echo "Dry run — nothing changed.  Re-run with --confirm once the siblings are pushed."
    exit 0
fi

if [[ -n "$(git status --porcelain)" ]]; then
    echo "Working tree is dirty — commit or stash first." >&2
    exit 1
fi

for package in "${PACKAGES[@]}"; do
    [[ -d "${package}" ]] && git rm -r --quiet "${package}"
done
for glob in "${TEST_GLOBS[@]}"; do
    for path in ${glob}; do
        [[ -e "${path}" ]] && git rm --quiet "${path}"
    done
done

echo
echo "Removed.  Core now ships the contract, the reference engine and the TCK."
echo "Run the suite, then commit."
