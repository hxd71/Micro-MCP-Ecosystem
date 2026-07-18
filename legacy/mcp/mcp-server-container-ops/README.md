# mcp-server-container-ops

Container diagnostics MCP server for AI Infra Ops Agent.

It checks Docker containers, logs, health, port mappings, mounts, environment variables, and high-risk restart plans. When Docker is unavailable, it serves a bundled `vllm-qwen` fixture so the offline demo is reproducible.
