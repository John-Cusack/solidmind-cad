# MCP Spec Gatherer (3D Print + CNC)

Deterministic, tool-driven specification gathering for manufactured parts.
Main use case: `print_3d` (FDM/FFF). Secondary supported process: `cnc`.

This repo implements:
- JSON Schema shape validation per process (`cnc`, `print_3d`)
- Question-bank driven coverage scoring
- Deterministic rules (blockers + warnings)
- Deterministic next-question selection
- Finalization: clean `spec.json` + RFC 8785-style canonicalization + SHA-256 hash
- Simple exports: design brief + RFQ summary (Markdown)

## What to provide up front (fast path)

If you answer these early, the assistant moves much faster:
1. Process (`print_3d` or `cnc`) and maturity (`L1`/`L2`/`L3`).
2. What the part does and what must fit or align.
3. Quantity and envelope limits.
4. Material preference (or functional constraints).
5. Interface details (threads/inserts/snaps/holes/mating faces).
6. Tolerance/fit expectations for critical features.
7. Appearance/finish expectations.
8. Deliverables needed (STL/3MF/STEP, drawing, etc.).
9. For in-house printing: key slicer/settings notes.

## Local usage

Run the stdio JSON-RPC server:
```bash
python3 -m server.main
```

Run unit tests:
```bash
python3 -m unittest
```

Replay a golden transcript:
```bash
python3 scripts/replay_transcript.py tests/transcripts/print_3d_L2.yml
```

## FreeCAD (optional)

If you have FreeCAD installed, you can generate a minimal CAD stub (an envelope box) from a finalized spec:
```bash
python3 scripts/freecad_from_spec.py --spec examples/print_3d/L2.json --out /tmp/part.step
```
