#!/usr/bin/env python3
"""Batch ingestion CLI for the SolidMind knowledge base.

Usage:
    python scripts/ingest_knowledge.py me_knowledge/notes/
    python scripts/ingest_knowledge.py ~/nasa-reports/
    python scripts/ingest_knowledge.py ~/docs/turbine-handbook.pdf
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure the project root is on sys.path so ``server.*`` imports work
# when running this script directly.
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from server.knowledge_store import KnowledgeStore, _make_embedding_fn, _DEFAULT_DB_PATH  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ingest files into the SolidMind knowledge base (LanceDB).",
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
        "--db-path",
        default=_DEFAULT_DB_PATH,
        help=f"LanceDB database path (default: {_DEFAULT_DB_PATH})",
    )
    args = parser.parse_args(argv)

    target = Path(args.path).expanduser().resolve()
    if not target.exists():
        print(f"ERROR: Path not found: {target}", file=sys.stderr)
        return 1

    print("Initializing knowledge store...")
    embedding_fn = _make_embedding_fn()
    store = KnowledgeStore(db_path=args.db_path, embedding_fn=embedding_fn)

    exts = tuple(args.extensions)

    if target.is_file():
        print(f"Ingesting {target.name}...")
        result = store.ingest_file(target)
        print(f"  -> status={result.status}  chunks={result.chunks}")
        if result.status == "failed":
            return 1
    elif target.is_dir():
        print(f"Scanning {target} for {', '.join(exts)} files...")
        results = store.ingest_directory(target, extensions=exts)
        if not results:
            print("No matching files found.")
            return 0
        succeeded = sum(1 for r in results if r.status == "complete")
        failed = sum(1 for r in results if r.status == "failed")
        for r in results:
            status_icon = "OK  " if r.status == "complete" else "FAIL"
            print(f"  {status_icon}  {r.filename}  chunks={r.chunks}")
        print(f"\nSucceeded: {succeeded}  Failed: {failed}")
    else:
        print(f"ERROR: {target} is neither a file nor directory.", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
