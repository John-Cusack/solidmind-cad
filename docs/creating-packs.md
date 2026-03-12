# Creating Extension Packs for SolidMind CAD

Extension packs add geometry/analysis tools and curated engineering knowledge to SolidMind CAD without modifying core code. Install a pack with `pip install` and restart the MCP server — new tools and knowledge appear automatically.

## Pack types

| Type | Adds | Interface |
|------|------|-----------|
| **Tool pack** | MCP tools (geometry calculators, analysis) | `TOOLS` list + `DISPATCH` dict |
| **Knowledge pack** | Curated engineering knowledge (design rules, material tables) | `KNOWLEDGE_DIR` path + `DOMAIN` string + `VERSION` string |
| **Combined pack** | Both | All five attributes |

## Quick start

### 1. Create the directory structure

```
solidmind-sheetmetal/
├── pyproject.toml
├── solidmind_sheetmetal/
│   ├── __init__.py          # empty
│   ├── pack.py              # TOOLS + DISPATCH (+ optional KNOWLEDGE_DIR/DOMAIN/VERSION)
│   └── tools.py             # your tool functions
```

### 2. Write your tool function

```python
# solidmind_sheetmetal/tools.py
import math

def bend_allowance(
    *,
    material_thickness: float,
    bend_angle_deg: float,
    bend_radius: float,
    k_factor: float = 0.33,
) -> dict:
    ba = math.pi / 180 * bend_angle_deg * (bend_radius + k_factor * material_thickness)
    return {"ok": True, "bend_allowance_mm": round(ba, 3), "k_factor": k_factor}
```

Tool functions receive keyword arguments matching the `inputSchema` and return a dict. Return `{"ok": True, ...}` on success or `{"ok": False, "error": {"code": "...", "message": "..."}}` on failure.

For tools that generate sketch geometry, import `from server.geometry_store import store` and return a `geometry_ref` handle — same pattern as core geometry tools.

### 3. Write `pack.py`

```python
# solidmind_sheetmetal/pack.py
from solidmind_sheetmetal.tools import bend_allowance

TOOLS: list[dict] = [
    {
        "name": "geometry.bend_allowance",
        "description": "Calculate sheet metal bend allowance and flat pattern length",
        "inputSchema": {
            "type": "object",
            "properties": {
                "material_thickness": {"type": "number"},
                "bend_angle_deg": {"type": "number"},
                "bend_radius": {"type": "number"},
                "k_factor": {"type": "number", "default": 0.33},
            },
            "required": ["material_thickness", "bend_angle_deg", "bend_radius"],
        },
    },
]

DISPATCH: dict = {"geometry.bend_allowance": bend_allowance}
```

### 4. Register entry points in `pyproject.toml`

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "solidmind-sheetmetal"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["solidmind-cad>=0.2.0"]

[project.entry-points."solidmind.tool_packs"]
sheetmetal = "solidmind_sheetmetal.pack"
```

### 5. Install and use

```bash
pip install -e .
# Restart MCP server — "geometry.bend_allowance" is now available
```

## Adding knowledge

Add these attributes to `pack.py`:

```python
from pathlib import Path

KNOWLEDGE_DIR: Path = Path(__file__).parent / "knowledge"
DOMAIN: str = "sheetmetal"
VERSION: str = "1.0.0"
```

Create a `knowledge/` directory with markdown files using `##` headers for semantic chunking:

```markdown
---
domain: sheetmetal
topic: bend_allowance
confidence: textbook
source: "Machinery's Handbook, 31st ed"
---

## Bend allowance formula
BA = (pi / 180) * angle * (R + K * T)
...
```

Register the knowledge entry point:

```toml
[project.entry-points."solidmind.knowledge_packs"]
sheetmetal = "solidmind_sheetmetal.pack"
```

Knowledge files auto-ingest into LanceDB on first `knowledge.search`. Bumping `VERSION` triggers re-ingestion.

## Entry point groups

| Group | Purpose |
|-------|---------|
| `solidmind.tool_packs` | Discovered at server startup, tools added to MCP tool list |
| `solidmind.knowledge_packs` | Lazily ingested on first search, version-tracked |

## Conventions

- Tool names should be namespaced: `geometry.`, `analysis.`, etc.
- Core tools always take priority — packs cannot override built-in tools.
- Knowledge markdown should use `##` headers for chunking and optional YAML frontmatter for metadata.
- A broken pack logs an error but doesn't crash the server.

## Example

See `examples/solidmind-example-pack/` for a working combined tool + knowledge pack.
