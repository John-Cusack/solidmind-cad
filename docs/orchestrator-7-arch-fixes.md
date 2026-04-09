# Orchestrator Code Review: 7 Architectural Fixes

## Context

After implementing the 5-phase orchestrator wiring (G3 gate, trust boundary, measured→scores, skeleton context, release packaging), a self-review identified 7 architectural issues in the new code: 1 bug, 2 DRY violations, 1 encapsulation breach, 1 type-safety gap, 1 dead code path, and 1 consistency gap. All 7 have been applied. This document is for an independent LLM reviewer to verify correctness.

---

## Fix 1: BUG — `or` falsiness trap in score fallback (sbce.py)

### Problem

Three call sites used `v.scores.get(obj.name) or v.measured.get(obj.name)` to fall back from `scores` to `measured`. In Python, `0.0` is falsy, so a legitimate score of `0.0` (e.g., zero mass, zero cost) would be silently skipped and the `measured` value used instead — or `None` if `measured` also lacks the key. This is a real data-corruption bug for any objective that can validly be zero.

### Fix applied

Replaced `or` with explicit `None` check at all 3 sites:

**sbce.py:63–65** — `filter_feasible()`:
```python
score = v.scores.get(obj.name)
if score is None:
    score = v.measured.get(obj.name)
if obj.threshold is not None and score is not None:
```

**sbce.py:212–215** — `_score_partial()`:
```python
score = v.scores.get(obj.name)
if score is None:
    score = v.measured.get(obj.name)
if score is not None:
```

**sbce.py:265–268** — `pareto_frontier._obj_values()`:
```python
score = v.scores.get(obj.name)
if score is None:
    score = v.measured.get(obj.name)
if score is not None:
```

### What to verify

- Confirm no remaining `or` pattern for score/measured fallback anywhere in `sbce.py` or `scorer.py`.
- Confirm a `Variant(scores={"mass": 0.0}, measured={"mass": 999})` would use `0.0`, not `999`.

---

## Fix 2: DRY — `_build_skeleton_section` duplicated in runner.py and worker_subprocess.py

### Problem

`runner.py` and `worker_subprocess.py` each had their own ~40-line `_build_skeleton_section()` function building the `## Spatial Constraints` prompt section. The logic was identical: look up reserved volumes, resolve datum refs from assembly_constraints, find overlapping keepout zones.

### Fix applied

Deleted the copy in `runner.py`. Now imports from `worker_subprocess.py`:

**runner.py:241**:
```python
from orchestrator.worker_subprocess import _build_skeleton_section
```

The canonical implementation lives at **worker_subprocess.py:131–172**.

### What to verify

- `runner.py` has no local `_build_skeleton_section` function definition.
- `worker_subprocess.py:_build_skeleton_section` is the single source of truth.
- Both prompt templates (`_WORKER_PROMPT` in runner.py and `_WORKER_PROMPT_TEMPLATE` in worker_subprocess.py) reference `{skeleton_section}`.

---

## Fix 3: DRY — `_extract_variant_index_from_worker_id` duplicated in release.py

### Problem

`release.py` had a standalone `_extract_variant_index_from_worker_id()` that was identical to `scorer.py:_extract_variant_index()`. Both parse a worker_id like `"sun_gear_0"` → `0`.

### Fix applied

**release.py:101–104** now delegates:
```python
def _extract_variant_index_from_worker_id(worker_id: str) -> int:
    """Extract variant index from worker_id like 'sun_gear_0'."""
    from orchestrator.scorer import _extract_variant_index
    return _extract_variant_index(worker_id)
```

### What to verify

- `release.py` contains no parsing logic (no `rsplit`, no `int()` call) in that function.
- `scorer.py:_extract_variant_index` (lines 108–116) contains the canonical implementation.

---

## Fix 4: Encapsulation — private `_aabb_overlap`/`_aabb_bounds` imported externally

### Problem

`skeleton.py` defined `_aabb_overlap()` and `_aabb_bounds()` with underscore prefixes (Python convention for module-private). But `validator.py`, `runner.py`, and `worker_subprocess.py` all imported them. This violates the convention that underscore-prefixed names are internal implementation details.

### Fix applied

1. Renamed functions to public names in **skeleton.py**:
   - `_aabb_overlap` → `aabb_overlap` (line 108)
   - `_aabb_bounds` → `aabb_bounds` (line 128)

2. Added backward-compatible aliases at **skeleton.py:147–149**:
   ```python
   # Backward-compatible aliases
   _aabb_overlap = aabb_overlap
   _aabb_bounds = aabb_bounds
   ```

3. Updated external imports to use public names:
   - **validator.py:306**: `from orchestrator.skeleton import aabb_overlap, aabb_bounds`
   - **worker_subprocess.py:162**: `from orchestrator.skeleton import aabb_overlap`

### What to verify

- No external module imports the underscore-prefixed names (`_aabb_overlap`, `_aabb_bounds`).
- Internal call sites within `skeleton.py` itself also use the public names.
- The backward-compatible aliases exist for any third-party code that may reference the old names.

---

## Fix 5: Type safety — `skeleton_checks` used untyped `dict` instead of a dataclass

### Problem

Every other validation check type (`DimensionCheck`, `EnvelopeCheck`, `ClearanceCheck`) uses a frozen `@dataclass(slots=True)`, but `skeleton_checks` was `list[dict[str, Any]]`. This means:
- No IDE autocompletion or type checking
- Key typos (`"passsed"` instead of `"passed"`) silently produce bugs
- Inconsistent with the rest of the validation module

### Fix applied

1. **New dataclass** at **validator.py:69–77**:
   ```python
   @dataclass(slots=True)
   class SkeletonCheck:
       check: str  # "reserved_volume" | "keepout_zone"
       subsystem: str = ""
       passed: bool = False
       error: str = ""
       keepout: str = ""
   ```

2. **ValidationReport field** at **validator.py:97** changed from `list[dict[str, Any]]` to `list[SkeletonCheck]`.

3. **`validate_skeleton_constraints()`** return type changed to `list[SkeletonCheck]`, returns `SkeletonCheck(...)` instances.

4. **`_compute_overall()`** at **validator.py:288–292** uses attribute access:
   ```python
   for sc in report.skeleton_checks:
       if not sc.passed:  # was: sc.get("passed", True)
   ```

5. **`report_to_dict()`** at **validator.py:431–440** serializes via attribute access:
   ```python
   "skeleton_checks": [
       {"check": sc.check, "subsystem": sc.subsystem, "passed": sc.passed, ...}
       for sc in report.skeleton_checks
   ]
   ```

6. **Tests** in `test_validator.py` updated from `sc.get("passed", True)` to `sc.passed`.

### What to verify

- `ValidationReport.skeleton_checks` type annotation is `list[SkeletonCheck]`, not `list[dict]`.
- No `.get("passed", ...)` dict-style access remains for skeleton checks anywhere in `validator.py` or tests.
- JSON serialization output format is unchanged (same keys/values).

---

## Fix 6: Dead code path — `runner.check_gate_g7` doesn't forward `spec`

### Problem

`release.check_gate_g7()` was updated to accept an optional `spec` parameter for checking purchased parts. But the runner facade `runner.check_gate_g7()` only accepted `release_package` and didn't pass `spec` through. The new purchased-parts check was therefore unreachable through the runner API.

### Fix applied

**runner.py:377–383**:
```python
def check_gate_g7(
    release_package: object,
    spec: MasterSpec | None = None,   # ← added
) -> tuple[bool, list[str]]:
    """G7: Release package completeness."""
    from orchestrator.release import check_gate_g7 as _g7
    return _g7(release_package, spec=spec)  # ← forwarded
```

### What to verify

- `runner.check_gate_g7` signature includes `spec: MasterSpec | None = None`.
- The `spec` parameter is forwarded: `_g7(release_package, spec=spec)`.
- Backward compatible: existing callers passing only `release_package` still work (default `None`).

---

## Fix 7: Inconsistency — Pareto frontier and G6 gate ignored `measured` fallback

### Problem

`_score_partial()` and `filter_feasible()` were updated to check `v.measured` as fallback when `v.scores` doesn't have an objective. But two other consumers were not updated:
1. `_obj_values()` inside `pareto_frontier()` (sbce.py) — only checked `v.scores`
2. `check_gate_g6()` threshold loop (scorer.py) — only checked `v.scores`

This means data visible during scoring/filtering could be invisible during Pareto ranking and the G6 gate, causing incorrect results.

### Fix applied

**sbce.py:264–268** — `_obj_values()` now checks measured:
```python
for v in c.variants.values():
    score = v.scores.get(obj.name)
    if score is None:
        score = v.measured.get(obj.name)
    if score is not None:
        total += score
        count += 1
```

**scorer.py:414–418** — `check_gate_g6()` threshold loop now checks measured:
```python
for v in candidate.variants.values():
    score = v.scores.get(obj.name)
    if score is None:
        score = v.measured.get(obj.name)
    if score is not None:
        values.append(score)
```

**scorer.py:429–430** — Guard also checks measured:
```python
if not any_passes and any(
    obj.name in v.scores or obj.name in v.measured
```

### What to verify

- All 5 locations that look up objective values from a Variant now use the same pattern:
  1. `filter_feasible()` in sbce.py
  2. `_score_partial()` in sbce.py
  3. `_obj_values()` in sbce.py
  4. `check_gate_g6()` threshold loop in scorer.py
  5. `check_gate_g6()` "has any data" guard in scorer.py
- No location uses the bare `if obj.name in v.scores` without also considering `v.measured`.

---

## Files Modified

| File | Fixes Applied |
|------|--------------|
| `orchestrator/sbce.py` | #1 (3 sites), #7 (_obj_values) |
| `orchestrator/runner.py` | #2 (import), #6 (g7 spec) |
| `orchestrator/release.py` | #3 (delegate) |
| `orchestrator/skeleton.py` | #4 (rename + aliases) |
| `orchestrator/validator.py` | #5 (SkeletonCheck dataclass) |
| `orchestrator/scorer.py` | #7 (g6 measured fallback) |
| `orchestrator/worker_subprocess.py` | #4 (public import name) |
| `tests/test_validator.py` | #5 (attribute access) |

## Test Verification

```bash
# All 135 orchestrator tests pass
python3 -m unittest tests.test_runner tests.test_validator tests.test_sbce \
  tests.test_release tests.test_interface_freeze tests.test_skeleton \
  tests.test_worker tests.test_orchestrator_cli tests.test_orchestrator_e2e
```
