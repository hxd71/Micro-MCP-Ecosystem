# mcp-server-ai-serving-ops

AI serving diagnostics and safe config repair MCP server.

The first version focuses on OpenAI-compatible services such as vLLM, Ollama, Triton, MindIE, and FastAPI wrappers. It validates model paths, parses serving logs, checks service health, suggests safe config patches, and supports backup/dry-run/rollback guarded by the Hub approval layer.
