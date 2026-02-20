#!/usr/bin/env python3
"""Summarize each doc in me_knowledge/sim_changes/ using the Claude Code CLI,
then produce a grouped summary."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


SIM_CHANGES_DIR = Path(__file__).resolve().parent.parent / "me_knowledge" / "sim_changes"
OUTPUT_FILE = SIM_CHANGES_DIR.parent / "sim_changes_summary.md"

SUMMARIZE_PROMPT = (
    "Summarize this document in 3-5 bullet points. "
    "Include: the main topic, key technical details, and what it's useful for. "
    "Also suggest a topic category from this list: "
    "Isaac Sim & Simulation Runtime, URDF & Articulation, Reinforcement Learning, "
    "Locomotion & Gait Control, FEA & Structural Validation, Mechanical Design & Materials. "
    "Output JSON: {\"category\": \"...\", \"title\": \"...\", \"bullets\": [\"...\", ...]}"
)

COMBINE_PROMPT = (
    "You are given per-file summaries of a knowledge base, grouped by topic. "
    "Write a clean markdown summary document with: "
    "1) A top-level heading and doc count, "
    "2) A table of contents, "
    "3) One section per topic group with a 2-3 sentence overview followed by per-file summaries. "
    "Output raw markdown only, no code fences."
)


def _claude_env() -> dict[str, str]:
    """Return a copy of os.environ without CLAUDECODE so nested calls work."""
    import os
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    return env


def summarize_file(filepath: Path) -> dict:
    """Call `claude` CLI to summarize a single file."""
    print(f"  Summarizing: {filepath.name} ...", flush=True)
    result = subprocess.run(
        ["claude", "-p", f"{SUMMARIZE_PROMPT}\n\nFile: {filepath.name}", "--output-format", "text"],
        input=filepath.read_text(encoding="utf-8", errors="replace"),
        capture_output=True,
        text=True,
        timeout=120,
        env=_claude_env(),
    )
    if result.returncode != 0:
        print(f"    WARN: claude returned {result.returncode}: {result.stderr.strip()}")
        return {
            "filename": filepath.name,
            "category": "Other",
            "title": filepath.stem,
            "bullets": ["(summarization failed)"],
        }

    raw = result.stdout.strip()
    # Try to parse JSON from the response (claude may wrap it in markdown fences)
    for attempt in [raw, _extract_json_block(raw)]:
        try:
            data = json.loads(attempt)
            data["filename"] = filepath.name
            data["word_count"] = len(filepath.read_text().split())
            return data
        except (json.JSONDecodeError, TypeError):
            continue

    # Fallback: use raw text as a single bullet
    print(f"    WARN: could not parse JSON, using raw text")
    return {
        "filename": filepath.name,
        "category": "Other",
        "title": filepath.stem,
        "bullets": [raw[:500]],
        "word_count": len(filepath.read_text().split()),
    }


def _extract_json_block(text: str) -> str | None:
    """Extract JSON from ```json ... ``` fences."""
    if "```" in text:
        for block in text.split("```"):
            block = block.strip()
            if block.startswith("json"):
                block = block[4:].strip()
            if block.startswith("{"):
                return block
    return None


def build_per_file_md(s: dict) -> str:
    """Build markdown for one file summary."""
    lines = [f"#### {s.get('title', s['filename'])}"]
    wc = s.get("word_count", "?")
    wc_str = f"{wc:,}" if isinstance(wc, int) else str(wc)
    lines.append(f"*File: `{s['filename']}` — {wc_str} words*\n")
    for b in s.get("bullets", []):
        lines.append(f"- {b}")
    lines.append("")
    return "\n".join(lines)


def combine_with_claude(groups: dict[str, list[dict]]) -> str:
    """Call claude CLI to write the final combined summary."""
    # Build a structured input for the combine step
    sections = []
    for topic, docs in groups.items():
        section = f"## {topic}\n"
        for d in docs:
            section += build_per_file_md(d) + "\n"
        sections.append(section)

    input_text = "\n---\n".join(sections)

    print("\n  Combining into final summary ...", flush=True)
    result = subprocess.run(
        ["claude", "-p", COMBINE_PROMPT, "--output-format", "text"],
        input=input_text,
        capture_output=True,
        text=True,
        timeout=120,
        env=_claude_env(),
    )
    if result.returncode != 0:
        print(f"    WARN: combine step failed ({result.returncode}), using raw grouped output")
        return input_text

    return result.stdout.strip()


def main() -> None:
    files = sorted(SIM_CHANGES_DIR.glob("*.md"))
    if not files:
        print("No markdown files found in", SIM_CHANGES_DIR)
        return

    print(f"Processing {len(files)} files via claude CLI...\n")

    # Step 1: Summarize each file one by one
    summaries: list[dict] = []
    for f in files:
        s = summarize_file(f)
        summaries.append(s)
        cat = s.get("category", "Other")
        print(f"    -> [{cat}] {s.get('title', f.stem)}")

    # Step 2: Group by category
    groups: dict[str, list[dict]] = {}
    for s in summaries:
        cat = s.get("category", "Other")
        groups.setdefault(cat, []).append(s)

    print(f"\n  {len(groups)} topic groups found:")
    for topic, docs in groups.items():
        print(f"    {topic}: {len(docs)} docs")

    # Step 3: Combine into final summary via claude
    final_md = combine_with_claude(groups)

    OUTPUT_FILE.write_text(final_md, encoding="utf-8")
    print(f"\nWrote summary to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
