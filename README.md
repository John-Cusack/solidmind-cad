# MCP Spec Gatherer (CNC MVP)

Deterministic, tool-driven specification gathering for CNC parts.

This repo implements the tool layer described in `spec_guide2.md`:
- JSON Schema shape validation (CNC-only)
- Question-bank driven coverage scoring
- Deterministic rule findings (blockers + warnings)
- Deterministic next-question selection
- Finalization: clean `spec.json` + RFC 8785-style canonicalization + SHA-256 hash
- Simple exports: design brief + RFQ summary (Markdown)

## What to provide up front (fast path)

If you answer these in your first prompt, the assistant can move fast:
1. What is the part and what does it do?
2. Prototype vs production (L2 vs L3)?
3. Material preference (or constraints: strength/temperature/corrosion/weight/cost).
4. Overall size / envelope limits.
5. Interfaces (what does it mate to) and any critical patterns/features.
6. Overall precision needed (rough/medium/very precise).
7. Surface finish / coating requirements (and which surfaces are cosmetic).
8. Quantity.
9. Deliverables needed (STEP, drawings, etc.).

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
python3 scripts/replay_transcript.py tests/transcripts/cnc_L2.yml
```

## FreeCAD (optional)

If you have FreeCAD installed, you can generate a minimal CAD stub (an envelope box) from a finalized spec:
```bash
python3 scripts/freecad_from_spec.py --spec examples/cnc/L2.json --out /tmp/part.step
```
