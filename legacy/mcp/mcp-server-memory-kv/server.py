from __future__ import annotations

import argparse
import json
import threading
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("mcp-server-memory-kv")

BASE_DIR = Path(__file__).resolve().parent
STORE_PATH = BASE_DIR / "memory_store.json"
STORE_LOCK = threading.Lock()


def _ensure_store_exists() -> None:
    if STORE_PATH.exists():
        return
    STORE_PATH.write_text("{}\n", encoding="utf-8")


def _read_store() -> dict[str, str]:
    _ensure_store_exists()
    try:
        data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        data = {}

    if not isinstance(data, dict):
        return {}

    normalized: dict[str, str] = {}
    for key, value in data.items():
        normalized[str(key)] = str(value)
    return normalized


def _write_store(data: dict[str, str]) -> None:
    STORE_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


@mcp.tool()
async def save_variable(key: str, value: str) -> str:
    """Save or update a key-value pair in persistent JSON storage.

    Args:
        key: Variable name.
        value: Variable value.

    Returns:
        A summary string confirming write status.
    """
    normalized_key = key.strip()
    if not normalized_key:
        return "Save failed: key cannot be empty."

    with STORE_LOCK:
        data = _read_store()
        data[normalized_key] = value
        _write_store(data)

    return f"Saved key '{normalized_key}'."


@mcp.tool()
async def get_variable(key: str) -> str:
    """Get a value by key from persistent JSON storage.

    Args:
        key: Variable name.

    Returns:
        Value string if present, otherwise a not-found message.
    """
    normalized_key = key.strip()
    if not normalized_key:
        return "Get failed: key cannot be empty."

    with STORE_LOCK:
        data = _read_store()

    if normalized_key not in data:
        return f"Not found: '{normalized_key}'."

    return data[normalized_key]


@mcp.tool()
async def delete_variable(key: str) -> str:
    """Delete a key from persistent JSON storage.

    Args:
        key: Variable name.

    Returns:
        Summary string indicating whether deletion happened.
    """
    normalized_key = key.strip()
    if not normalized_key:
        return "Delete failed: key cannot be empty."

    with STORE_LOCK:
        data = _read_store()
        if normalized_key not in data:
            return f"Not found: '{normalized_key}'."
        del data[normalized_key]
        _write_store(data)

    return f"Deleted key '{normalized_key}'."


@mcp.tool()
async def list_variables_by_prefix(prefix: str) -> str:
    """List key-value pairs whose key starts with prefix.

    Args:
        prefix: Key prefix for filtering.

    Returns:
        JSON string of matched key-value pairs.
    """
    normalized_prefix = prefix.strip()

    with STORE_LOCK:
        data = _read_store()

    matched = {
        key: value for key, value in sorted(data.items()) if key.startswith(normalized_prefix)
    }
    return json.dumps(matched, ensure_ascii=False, indent=2)


@mcp.tool()
async def export_variables() -> str:
    """Export all key-value pairs as JSON.

    Returns:
        Full JSON content from memory store.
    """
    with STORE_LOCK:
        data = _read_store()

    return json.dumps(dict(sorted(data.items())), ensure_ascii=False, indent=2)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run mcp-server-memory-kv")
    parser.add_argument(
        "--transport",
        choices=["stdio"],
        default="stdio",
        help="Transport mode. This server is intended for stdio in mcp-core-hub.",
    )
    _ = parser.parse_args()

    _ensure_store_exists()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
