# Knowledge Backend (LanceDB + Docling)

Runs fully in-process — no Docker. LanceDB for hybrid search, Docling for PDF/DOCX extraction, embeddings from Ollama (GPU) or sentence-transformers (CPU fallback).

```bash
python scripts/ingest_knowledge.py me_knowledge/notes/
python scripts/ingest_knowledge.py ~/some-pdfs/
```

## Environment Variables

- `OLLAMA_URL` — Ollama base URL (e.g., `http://localhost:11434`)
- `EMBEDDING_MODEL` — Model name (default: `nomic-embed-text` / `all-MiniLM-L6-v2`)
- `KNOWLEDGE_DB_PATH` — Override LanceDB path (default: `me_knowledge/lancedb/`)

When dependencies are missing, `knowledge.*` tools fall back to listing local `me_knowledge/notes/` files.
