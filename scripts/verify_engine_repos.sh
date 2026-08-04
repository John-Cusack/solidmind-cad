#!/usr/bin/env bash
# Verify each split-out engine repository stands on its own.
#
# "Stands alone" is the whole claim of the split, so it gets checked rather
# than asserted: each repo is exercised from *its own directory*, with core
# nowhere on the path.  Three things per repo:
#
#   1. history survived      — more than the scaffold commit
#   2. import guard passes   — nothing imports server.*
#   3. own tests pass        — and, for engines, the TCK against a live bridge
#
# Usage: scripts/verify_engine_repos.sh [--dest DIR]

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$(dirname "${REPO_ROOT}")"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dest) DEST="$2"; shift 2 ;;
        -h|--help) sed -n '2,14p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; exit 2 ;;
    esac
done

# repo | module to launch for the TCK (empty = no bridge) | port | extra argv
repos=(
"solidmind-engine-isaac|isaac_bridge.bridge_server|19878|--headless"
"solidmind-engine-gazebo|gazebo_bridge.bridge_server|19879|--runtime stub"
"solidmind-engine-chrono||0|"
"solidmind-rl||0|"
)

failures=0

for spec in "${repos[@]}"; do
    IFS='|' read -r name module port launch_args <<<"${spec}"
    target="${DEST}/${name}"
    echo "=== ${name}"

    if [[ ! -d "${target}/.git" ]]; then
        echo "  MISSING — run scripts/split_engines.sh first"
        failures=$((failures + 1))
        continue
    fi

    commits="$(git -C "${target}" rev-list --count HEAD)"
    if [[ "${commits}" -le 1 ]]; then
        echo "  FAIL history: only ${commits} commit(s) — the split lost it"
        failures=$((failures + 1))
    else
        echo "  ok   history: ${commits} commits, oldest $(git -C "${target}" log --reverse --format=%ad --date=short | head -1)"
    fi

    # PYTHONPATH is deliberately *not* set to core: an engine that needs core
    # on the path has not actually been severed.
    if (cd "${target}" && python3 -m unittest discover -s tests -t . >/tmp/tck_verify.log 2>&1); then
        echo "  ok   tests: $(grep -oE 'Ran [0-9]+ test' /tmp/tck_verify.log | tail -1)"
    else
        echo "  FAIL tests:"
        tail -15 /tmp/tck_verify.log | sed 's/^/       /'
        failures=$((failures + 1))
    fi

    if [[ -n "${module}" ]]; then
        (cd "${target}" && python3 -m "${module}" --port "${port}" ${launch_args} >/dev/null 2>&1 &)
        sleep 2
        if (cd "${target}" && python3 -m tck --port "${port}" --latency-samples 10 >/tmp/tck_run.log 2>&1); then
            echo "  ok   TCK: conformant"
        else
            echo "  FAIL TCK:"
            grep -E "FAIL|RESULT" /tmp/tck_run.log | sed 's/^/       /'
            failures=$((failures + 1))
        fi
        pkill -f "${module} --port ${port}" 2>/dev/null || true
    fi
done

echo
if [[ "${failures}" -eq 0 ]]; then
    echo "All engine repositories stand alone."
else
    echo "${failures} check(s) failed."
    exit 1
fi
