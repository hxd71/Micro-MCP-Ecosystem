from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import BaseModel, Field, create_model

try:
    from langchain_core.messages import HumanMessage
    from langchain_core.tools import StructuredTool
    from langchain_openai import ChatOpenAI
    from langgraph.prebuilt import create_react_agent

    HAS_LLM_STACK = True
except ImportError:
    HumanMessage = None  # type: ignore[assignment]
    ChatOpenAI = None  # type: ignore[assignment]
    create_react_agent = None  # type: ignore[assignment]
    HAS_LLM_STACK = False

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"


@dataclass
class ConnectedServer:
    name: str
    session: ClientSession
    tools: list[Any]


@dataclass
class LocalTool:
    name: str
    description: str
    args_schema: type[BaseModel] | None
    _callable: Callable[..., Awaitable[str]]

    async def ainvoke(self, arguments: dict[str, Any]) -> str:
        return await self._callable(**arguments)


def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as config_file:
        return json.load(config_file)


def resolve_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return (BASE_DIR / path).resolve()


def json_schema_type_to_python_type(schema: dict[str, Any]) -> type[Any]:
    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        schema_type = next((item for item in schema_type if item != "null"), "string")

    mapping: dict[str, type[Any]] = {
        "string": str,
        "number": float,
        "integer": int,
        "boolean": bool,
        "array": list[Any],
        "object": dict[str, Any],
    }
    return mapping.get(schema_type, Any)


def build_args_model(tool_name: str, input_schema: dict[str, Any] | None) -> type[BaseModel]:
    schema = input_schema or {}
    properties = schema.get("properties", {}) or {}
    required = set(schema.get("required", []) or [])

    fields: dict[str, tuple[Any, Any]] = {}
    for field_name, field_schema in properties.items():
        field_type = json_schema_type_to_python_type(field_schema)
        if field_name not in required:
            field_type = field_type | None  # type: ignore[operator]
            default_value = field_schema.get("default", None)
        else:
            default_value = ...

        fields[field_name] = (
            field_type,
            Field(default=default_value, description=field_schema.get("description", "")),
        )

    if not fields:
        return create_model(f"{tool_name.title().replace('-', '').replace('.', '')}Args")

    return create_model(
        f"{tool_name.title().replace('-', '').replace('.', '')}Args",
        **fields,
    )


def stringify_tool_result(result: Any) -> str:
    content = getattr(result, "content", result)
    parts: list[str] = []

    if isinstance(content, (list, tuple)):
        content_items = content
    else:
        content_items = [content]

    for item in content_items:
        if isinstance(item, str):
            parts.append(item)
            continue

        if isinstance(item, dict):
            if item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            else:
                parts.append(json.dumps(item, ensure_ascii=False, indent=2))
            continue

        text_value = getattr(item, "text", None)
        if text_value is not None:
            parts.append(str(text_value))
            continue

        parts.append(str(item))

    return "\n".join(part for part in parts if part is not None and part != "") or str(result)


async def connect_server(session_name: str, server_config: dict[str, Any], stack: AsyncExitStack) -> ConnectedServer:
    script_path = resolve_path(server_config["script"])
    command = server_config.get("command") or sys.executable
    args = [str(script_path), *server_config.get("args", [])]
    env = os.environ.copy()
    env.update(server_config.get("env", {}))

    server_params = StdioServerParameters(
        command=command,
        args=args,
        env=env,
    )

    read_write = await stack.enter_async_context(stdio_client(server_params))
    read, write = read_write
    session = await stack.enter_async_context(ClientSession(read, write))
    await session.initialize()

    tools_result = await session.list_tools()
    tools_source = getattr(tools_result, "tools", tools_result)
    tools: list[Any] = []

    for tool_spec in tools_source:
        tool_name = getattr(tool_spec, "name")
        tool_description = getattr(tool_spec, "description", "") or ""
        input_schema = getattr(tool_spec, "inputSchema", None) or getattr(tool_spec, "input_schema", None)
        args_model = build_args_model(tool_name, input_schema)

        async def call_mcp_tool(_tool_name: str = tool_name, **kwargs: Any) -> str:
            result = await session.call_tool(_tool_name, arguments=kwargs)
            return stringify_tool_result(result)

        full_tool_name = f"{session_name}.{tool_name}"
        if HAS_LLM_STACK:
            tools.append(
                StructuredTool.from_function(
                    coroutine=call_mcp_tool,
                    name=full_tool_name,
                    description=f"[{session_name}] {tool_description}".strip(),
                    args_schema=args_model,
                )
            )
        else:
            tools.append(
                LocalTool(
                    name=full_tool_name,
                    description=f"[{session_name}] {tool_description}".strip(),
                    args_schema=args_model,
                    _callable=call_mcp_tool,
                )
            )

    return ConnectedServer(name=session_name, session=session, tools=tools)


async def build_connections(stack: AsyncExitStack) -> list[ConnectedServer]:
    config = load_config()
    server_configs = [item for item in config.get("servers", []) if item.get("enabled", True)]

    connected_servers: list[ConnectedServer] = []
    for server_config in server_configs:
        connected_servers.append(
            await connect_server(server_config["name"], server_config, stack)
        )

    return connected_servers


def build_model(config: dict[str, Any]) -> Any:
    model_config = config.get("model", {})
    model_name = model_config.get(
        "model",
        os.environ.get("MCP_CORE_HUB_MODEL", "minimax-text-01"),
    )
    base_url = model_config.get(
        "base_url",
        os.environ.get("MINIMAX_BASE_URL", os.environ.get("OPENAI_BASE_URL", "https://api.minimaxi.chat/v1")),
    )
    api_key = model_config.get(
        "api_key",
        os.environ.get("MINIMAX_API_KEY", os.environ.get("OPENAI_API_KEY")),
    )

    if not api_key:
        raise RuntimeError(
            "请先设置 MINIMAX_API_KEY（或 OPENAI_API_KEY），并确认 base_url 可用。"
        )

    if ChatOpenAI is None:
        raise RuntimeError("当前环境未安装 langchain-openai，无法启用 LangGraph 模式。")

    return ChatOpenAI(
        model=model_name,
        temperature=float(model_config.get("temperature", 0)),
        base_url=base_url,
        api_key=api_key,
    )


def build_system_prompt(connected_servers: list[ConnectedServer]) -> str:
    tool_names = [tool.name for server in connected_servers for tool in server.tools]
    if tool_names:
        tool_block = "\n".join(f"- {name}" for name in tool_names)
    else:
        tool_block = "- (no tools loaded)"

    return (
        "你是 mcp-core-hub，一个集中式的 MCP 调用入口。\n"
        "你的职责是读取用户输入，必要时调用可用工具，再把结果清晰地返回给用户。\n"
        "优先使用工具解决问题，回答要简洁。\n"
        "当前可用工具如下：\n"
        f"{tool_block}"
    )


async def run_repl() -> None:
    config = load_config()

    async with AsyncExitStack() as stack:
        connected_servers = await build_connections(stack)
        all_tools = [tool for server in connected_servers for tool in server.tools]
        tool_map = {tool.name: tool for tool in all_tools}
        system_prompt = build_system_prompt(connected_servers)

        use_langgraph = HAS_LLM_STACK
        model_error: str | None = None
        agent: Any = None

        if use_langgraph:
            try:
                model = build_model(config)
                agent = create_react_agent(model, tools=all_tools, prompt=system_prompt)
            except Exception as exc:
                use_langgraph = False
                model_error = str(exc)

        if use_langgraph:
            print("mcp-core-hub 已启动（LangGraph 模式）。输入问题，或输入 exit/quit 退出。")
            messages: list[Any] = []

            while True:
                user_input = input("mcp-core-hub> ").strip()
                if user_input.lower() in {"exit", "quit"}:
                    break
                if not user_input:
                    continue

                messages.append(HumanMessage(content=user_input))
                result = await agent.ainvoke({"messages": messages})
                messages = result["messages"]
                last_message = messages[-1]
                print(getattr(last_message, "content", str(last_message)))
            return

        if model_error:
            print(f"[提示] 自动降级为离线测试模式：{model_error}")
        print("mcp-core-hub 已启动（离线测试模式）。输入 tools 查看工具，或用 call <tool_name> <json> 直接调用工具。")
        print(system_prompt)

        while True:
            user_input = input("mcp-core-hub> ").strip()
            if user_input.lower() in {"exit", "quit"}:
                break
            if not user_input:
                continue
            if user_input == "tools":
                for tool_name in sorted(tool_map):
                    print(tool_name)
                continue
            if user_input.startswith("call "):
                try:
                    _, remainder = user_input.split(" ", 1)
                    tool_name, raw_arguments = remainder.split(" ", 1)
                    arguments = json.loads(raw_arguments)
                    result = await tool_map[tool_name].ainvoke(arguments)
                    print(result)
                except Exception as exc:
                    print(f"调用失败: {exc}")
                continue

            print("离线模式下可用命令: tools | call <tool_name> <json> | exit")


if __name__ == "__main__":
    asyncio.run(run_repl())
