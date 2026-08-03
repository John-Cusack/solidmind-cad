#!/usr/bin/env bash
# Split the engine packages out of core into sibling repositories.
#
# Each engine keeps its own history: `git subtree split` rewrites only the
# commits that touched its files, so `git log` in the new repo shows the real
# story of that bridge rather than one squashed import.
#
# The operation is idempotent and non-destructive on core: it creates branches
# (split/<package>) and new directories, and changes nothing else.  Removing
# the packages from core is a separate, deliberate step —
# scripts/remove_engines_from_core.sh — to be run only once the siblings are
# pushed somewhere durable.
#
# Usage:
#   scripts/split_engines.sh [--dest DIR] [--force]
#
#   --dest DIR   Where the sibling repos go (default: the parent of this repo)
#   --force      Replace existing destination repos
#
# Layout produced (docs/engine-integration-architecture.md §4):
#
#   solidmind-engine-isaac    isaac_bridge/   + its tests
#   solidmind-engine-gazebo   gazebo_bridge/  + its tests
#   solidmind-engine-chrono   chrono_daemon/ + chrono_bridge/ + their tests
#   solidmind-rl              rl_training/    + its tests

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$(dirname "${REPO_ROOT}")"
FORCE=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dest) DEST="$2"; shift 2 ;;
        --force) FORCE=1; shift ;;
        -h|--help) sed -n '2,30p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; exit 2 ;;
    esac
done

cd "${REPO_ROOT}"

if [[ -n "$(git status --porcelain)" ]]; then
    echo "Working tree is dirty — commit or stash first." >&2
    exit 1
fi

TEMPLATE_DIR="${REPO_ROOT}/scripts/engine_repo_template"

# ---------------------------------------------------------------------------
# name | packages | test globs | description | extra launch argv
# ---------------------------------------------------------------------------
engine_specs=(
"solidmind-engine-isaac|isaac_bridge|test_isaac_*.py test_teleop_lifecycle.py|Isaac Sim engine for SolidMind — GPU contact physics, sessions and teleop.|--headless"
"solidmind-engine-gazebo|gazebo_bridge|test_gazebo_*.py test_package_to_sdf.py test_px4_airframe.py test_mavlink_controller.py|Gazebo engine for SolidMind — package→SDF compilation, PX4 SITL, 5-DOF teleop.|--runtime stub"
"solidmind-engine-chrono|chrono_daemon chrono_bridge|test_chrono_*.py|Project Chrono engine for SolidMind — analytic multibody dynamics behind a contract shim.|"
"solidmind-rl|rl_training|test_urdf_analyzer.py|Isaac Lab + RSL-RL training pipeline for SolidMind robots.|"
)

split_branch() {
    local package="$1"
    local branch="split/${package}"
    git rev-parse --verify --quiet "${branch}" >/dev/null && git branch -D "${branch}" >/dev/null
    echo "  splitting ${package} ..."
    git subtree split --prefix="${package}" --branch="${branch}" >/dev/null 2>&1
    echo "${branch}"
}

build_repo() {
    local name="$1" packages="$2" test_globs="$3" description="$4" launch_args="$5"
    local target="${DEST}/${name}"

    if [[ -e "${target}" ]]; then
        if [[ "${FORCE}" -eq 1 ]]; then
            rm -rf "${target}"
        else
            echo "  ${target} exists — skipping (use --force to replace)"
            return 0
        fi
    fi

    echo "${name}:"
    git init --quiet --initial-branch=main "${target}"

    local first=1
    for package in ${packages}; do
        local branch
        branch="$(split_branch "${package}" | tail -1)"

        git -C "${target}" fetch --quiet "${REPO_ROOT}" "${branch}"
        if [[ "${first}" -eq 1 ]]; then
            git -C "${target}" checkout --quiet -b main FETCH_HEAD
            first=0
        else
            # A second package joins as an unrelated history — both keep their
            # own commits, which is the point of splitting rather than copying.
            git -C "${target}" merge --quiet --allow-unrelated-histories \
                --no-edit FETCH_HEAD
        fi

        # subtree split puts the package's files at the repo root; put them
        # back under the package name so imports keep working unchanged.
        mkdir -p "${target}/${package}"
        (
            cd "${target}"
            for entry in *; do
                [[ "${entry}" == "${package}" ]] && continue
                # Leave anything already filed under another package alone.
                local skip=0
                for other in ${packages}; do
                    [[ "${entry}" == "${other}" ]] && skip=1
                done
                [[ "${skip}" -eq 1 ]] && continue
                git mv "${entry}" "${package}/"
            done
        )
        git -C "${target}" commit --quiet -m "chore: file ${package} under its package directory" || true
    done

    # ---- tests that belong to this engine ------------------------------
    #
    # A test that imports server.* cannot run here — that is the boundary the
    # split exists to draw.  Rather than shipping a red suite or dropping the
    # coverage, those land in tests/needs_porting/ with a note: they are real
    # work, enumerated, and core keeps its copies until they are done.
    mkdir -p "${target}/tests" "${target}/tests/needs_porting"
    local copied=0 deferred=0
    for glob in ${test_globs}; do
        for test_file in ${REPO_ROOT}/tests/${glob}; do
            [[ -e "${test_file}" ]] || continue
            if grep -qE "(^|[[:space:]])(from|import) server[. ]" "${test_file}"; then
                cp "${test_file}" "${target}/tests/needs_porting/"
                deferred=$((deferred + 1))
            else
                cp "${test_file}" "${target}/tests/"
                copied=$((copied + 1))
            fi
        done
    done
    cp "${REPO_ROOT}/tests/conftest.py" "${target}/tests/" 2>/dev/null || true
    echo "  ${copied} test module(s), ${deferred} needing a port"

    # ---- the conformance kit, vendored ---------------------------------
    # Engines run the TCK in their own CI; vendoring it keeps the repo
    # self-contained (tck/README.md).  Re-vendor when core's contract moves.
    cp -r "${REPO_ROOT}/tck" "${target}/tck"
    rm -rf "${target}/tck/__pycache__"
    mkdir -p "${target}/schemas"
    cp "${REPO_ROOT}/schemas/sim_result.schema.json" "${target}/schemas/"
    cp "${REPO_ROOT}/schemas/sim_package.schema.json" "${target}/schemas/"

    # ---- scaffolding ---------------------------------------------------
    if [[ "${deferred}" -gt 0 ]]; then
        cat > "${target}/tests/needs_porting/README.md" <<PORTING
# Tests that still reach into core

These came across in the split but import \`server.*\`, so they cannot run in
this repository as-is. That import is exactly what the split removes: an
engine repo has its own interpreter and shares nothing with core but the
contract.

Porting each one means swapping the core dependency for a local equivalent:

| Core import | Use instead |
|---|---|
| \`server.engine_client\` | the vendored \`tck.client.TckClient\`, or this engine's own client |
| \`server.motion_models\` | plain mechanism dicts — the wire format is dicts anyway |
| \`server.sim_export\` / \`server.sim_package_manifest\` | a fixture package under \`tests/fixtures/\` |

They are excluded from discovery (this directory has no \`__init__.py\`), so CI
stays green while the work happens. Core keeps its copies until then.
PORTING
    fi

    local primary_package="${packages%% *}"
    "${REPO_ROOT}/scripts/render_engine_repo.py" \
        --target "${target}" \
        --name "${name}" \
        --packages "${packages}" \
        --primary "${primary_package}" \
        --description "${description}" \
        --launch-args="${launch_args}"

    git -C "${target}" add -A
    git -C "${target}" commit --quiet -m "$(cat <<EOF
chore: scaffold ${name} as a standalone engine repository

Split out of solidmind-cad with history (git subtree split).  Adds the
packaging, README, CI and import guard the repo needs to stand alone, and
vendors the TCK so conformance runs here rather than in core.
EOF
)"
    echo "  -> ${target} ($(git -C "${target}" rev-list --count HEAD) commits)"
}

echo "Splitting engines into ${DEST}"
for spec in "${engine_specs[@]}"; do
    IFS='|' read -r name packages test_globs description launch_args <<<"${spec}"
    build_repo "${name}" "${packages}" "${test_globs}" "${description}" "${launch_args}"
done

cat <<'EOF'

Done.  Each repo has its own history, tests, CI and a vendored TCK.

Next:
  1. Verify:  scripts/verify_engine_repos.sh
  2. Publish: create the remotes and push each repo's main branch
  3. Only then: scripts/remove_engines_from_core.sh
EOF
