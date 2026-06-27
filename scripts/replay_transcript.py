from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

# Ensure repo root is on sys.path when running as a script.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from server.tools import (  # noqa: E402  must follow the sys.path bootstrap above
    spec_apply_answer,
    spec_assess_design_path,
    spec_export_brief,
    spec_export_rfq_summary,
    spec_finalize,
    spec_next_question,
    spec_select_schema,
    spec_validate,
)

TOOL_CALLS = {
    "spec.select_schema": spec_select_schema,
    "spec.apply_answer": spec_apply_answer,
    "spec.assess_design_path": spec_assess_design_path,
    "spec.validate": spec_validate,
    "spec.next_question": spec_next_question,
    "spec.finalize": spec_finalize,
    "spec.export_brief": spec_export_brief,
    "spec.export_rfq_summary": spec_export_rfq_summary,
}


class TranscriptError(RuntimeError):
    pass


def _subset_ok(expected: Any, actual: Any, path: str) -> None:
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            raise TranscriptError(f"Type mismatch at {path}: expected dict, got {type(actual).__name__}")
        for k, v in expected.items():
            if k not in actual:
                raise TranscriptError(f"Missing key at {path}: {k}")
            _subset_ok(v, actual[k], f"{path}.{k}")
        return

    if isinstance(expected, list):
        if not isinstance(actual, list):
            raise TranscriptError(f"Type mismatch at {path}: expected list, got {type(actual).__name__}")
        if len(expected) > len(actual):
            raise TranscriptError(f"List too short at {path}: expected >= {len(expected)}, got {len(actual)}")
        for i, v in enumerate(expected):
            _subset_ok(v, actual[i], f"{path}[{i}]")
        return

    if expected != actual:
        raise TranscriptError(f"Value mismatch at {path}: expected {expected!r}, got {actual!r}")


def replay(transcript_path: str) -> None:
    raw = yaml.safe_load(open(transcript_path, encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TranscriptError("Transcript must be a mapping")

    spec_draft = raw.get("spec_draft")
    if not isinstance(spec_draft, dict):
        raise TranscriptError("Transcript must include spec_draft (mapping)")

    steps = raw.get("steps")
    if not isinstance(steps, list):
        raise TranscriptError("Transcript must include steps (list)")

    last_spec: dict | None = None

    for i, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            raise TranscriptError(f"Step {i} must be a mapping")
        tool = step.get("tool")
        if tool not in TOOL_CALLS:
            raise TranscriptError(f"Step {i}: unknown tool {tool!r}")
        params = step.get("params") or {}
        if not isinstance(params, dict):
            raise TranscriptError(f"Step {i}: params must be a mapping")

        # Convenience injection.
        if tool in ("spec.apply_answer", "spec.validate", "spec.next_question", "spec.finalize"):
            params.setdefault("spec_draft", spec_draft)
        if tool in ("spec.export_brief", "spec.export_rfq_summary"):
            if "spec" not in params:
                if last_spec is None:
                    raise TranscriptError(f"Step {i}: no spec available for export tool")
                params["spec"] = last_spec

        out = TOOL_CALLS[tool](**params)

        if tool == "spec.apply_answer" and out.get("applied") is True:
            spec_draft = out.get("spec_draft_updated", spec_draft)
        if tool == "spec.finalize" and isinstance(out.get("spec"), dict):
            last_spec = out["spec"]

        expect_subset = step.get("expect_subset")
        if expect_subset is not None:
            _subset_ok(expect_subset, out, path=f"step[{i}].out")

    # Silence indicates success.


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Replay a golden transcript against the tool layer.")
    parser.add_argument("transcript", help="Path to transcript YAML (tests/transcripts/*.yml)")
    args = parser.parse_args(argv)

    try:
        replay(args.transcript)
    except TranscriptError as e:
        print(f"Transcript failed: {e}", file=sys.stderr)
        raise SystemExit(1) from e


if __name__ == "__main__":
    main()
