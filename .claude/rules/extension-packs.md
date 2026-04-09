# Extension Packs

Separate pip packages that add tools and/or knowledge at runtime. No core code changes needed.

- **Tool packs**: expose `TOOLS` + `DISPATCH`. Entry point: `solidmind.tool_packs`
- **Knowledge packs**: expose `KNOWLEDGE_DIR` + `DOMAIN` + `VERSION`. Entry point: `solidmind.knowledge_packs`
- **Combined packs**: expose all five attributes.
- **Solver packs**: expose `SOLVERS` list of `FieldSolver` instances. Entry point: `solidmind.solver_packs`. Can also expose `TOOLS`/`DISPATCH` and `KNOWLEDGE_DIR`/`DOMAIN`/`VERSION`.

Core tools always take priority. Broken packs log errors but don't crash the server.

See `docs/creating-packs.md` and `examples/solidmind-example-pack/`.
