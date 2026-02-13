#!/usr/bin/env python3
"""Batch ingestion CLI for the SolidMind knowledge base.

Usage:
    python scripts/ingest_knowledge.py me_knowledge/notes/
    python scripts/ingest_knowledge.py ~/nasa-reports/
    python scripts/ingest_knowledge.py ~/docs/turbine-handbook.pdf
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

# Ensure the project root is on sys.path so ``server.*`` imports work
# when running this script directly.
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from server.openrag_client import OpenRAGClient  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ingest files into the SolidMind knowledge base via OpenRAG.",
    )
    parser.add_argument(
        "path",
        help="File or directory to ingest. Directories are walked recursively.",
    )
    parser.add_argument(
        "--extensions",
        nargs="*",
        default=[".pdf", ".docx", ".md"],
        help="File extensions to include for directory ingestion (default: .pdf .docx .md)",
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("OPENRAG_URL", "http://localhost:8080"),
        help="OpenRAG API URL (default: $OPENRAG_URL or http://localhost:8080)",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("OPENRAG_API_KEY", ""),
        help="OpenRAG API key (default: $OPENRAG_API_KEY)",
    )
    parser.add_argument(
        "--poll",
        action="store_true",
        help="Wait for ingestion to complete and report final status.",
    )
    args = parser.parse_args(argv)

    client = OpenRAGClient(base_url=args.url, api_key=args.api_key)

    if not client.health_check():
        print(f"ERROR: Cannot reach OpenRAG at {args.url}", file=sys.stderr)
        print("Start OpenRAG first: bash scripts/setup_openrag.sh", file=sys.stderr)
        return 1

    target = Path(args.path).expanduser().resolve()
    if not target.exists():
        print(f"ERROR: Path not found: {target}", file=sys.stderr)
        return 1

    exts = tuple(args.extensions)
    task_ids: list[str] = []

    if target.is_file():
        print(f"Ingesting {target.name}...")
        try:
            result = client.ingest_file(target)
            print(f"  -> task_id={result.task_id}  status={result.status}")
            if result.task_id:
                task_ids.append(result.task_id)
        except Exception as e:
            print(f"  -> FAILED: {e}", file=sys.stderr)
            return 1
    elif target.is_dir():
        print(f"Scanning {target} for {', '.join(exts)} files...")
        results = client.ingest_directory(target, extensions=exts)
        if not results:
            print("No matching files found.")
            return 0
        succeeded = 0
        failed = 0
        for r in results:
            if r.status == "failed":
                print(f"  FAIL  {r.filename}")
                failed += 1
            else:
                print(f"  OK    {r.filename}  task_id={r.task_id}")
                succeeded += 1
                if r.task_id:
                    task_ids.append(r.task_id)
        print(f"\nSubmitted: {succeeded}  Failed: {failed}")
    else:
        print(f"ERROR: {target} is neither a file nor directory.", file=sys.stderr)
        return 1

    if args.poll and task_ids:
        print("\nPolling for completion...")
        remaining = set(task_ids)
        while remaining:
            time.sleep(2)
            done: list[str] = []
            for tid in remaining:
                try:
                    status = client.ingest_status(tid)
                    s = status.get("status", "unknown")
                    if s in ("complete", "failed"):
                        print(f"  {tid}: {s}")
                        done.append(tid)
                except Exception:
                    pass
            for tid in done:
                remaining.discard(tid)
            if remaining:
                print(f"  ... {len(remaining)} still processing")
        print("All tasks complete.")

    client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
