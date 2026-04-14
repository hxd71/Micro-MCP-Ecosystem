# mcp-server-rag-docs

A lightweight MCP RAG server for local docs.

## What It Does

- Reads files from `docs/` (`.md`, `.txt`, `.log`)
- Splits text chunks with LangChain text splitter
- Builds an in-memory FAISS vector index at startup
- Exposes MCP tools:
  - `search_knowledge_base(query: str)`
  - `refresh_knowledge_base()`

## Run Standalone

```bash
python server.py --transport stdio
```

## Docs Folder

Put your runbooks, troubleshooting notes, and API docs in:

- `docs/`

The server rebuilds index on startup.
Use `refresh_knowledge_base()` after adding/changing docs.
