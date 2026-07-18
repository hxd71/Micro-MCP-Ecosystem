# Legacy MCP compatibility modules

These modules are preserved for one compatibility release and for explicit demo/offline evaluation only.
They are not part of the `aiops-agent` daemon runtime and must not be used as an internal operations bus.

Production code lives in `src/aiops_agent/`. The only supported MCP-facing production compatibility
surface is the read-only root `server.py` shim; it exposes capability and manifest validation tools only.

The Hub and servers in this directory may use fixtures, generic tools, RAG, Memory KV, Ascend or NPU
logic that is intentionally outside the Linux + Docker + NVIDIA + vLLM v1 acceptance scope.
