# mcp-server-memory-kv

A lightweight MCP server for persistent key-value memory using a local JSON file.

## Tools

- save_variable(key: str, value: str)
- get_variable(key: str)
- delete_variable(key: str)
- list_variables_by_prefix(prefix: str)
- export_variables()

## Storage

- Backing file: memory_store.json
- Format: UTF-8 JSON object

## Run

```bash
python server.py --transport stdio
```

## Usage Examples

- save_variable("bug_reason", "数据库端口被占用")
- get_variable("bug_reason")
- list_variables_by_prefix("bug_")
- export_variables()
- delete_variable("bug_reason")
