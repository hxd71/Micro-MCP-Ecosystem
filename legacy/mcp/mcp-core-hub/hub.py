from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import BaseModel, Field, create_model

try:
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_core.tools import StructuredTool
    from langchain_openai import ChatOpenAI
    from langgraph.prebuilt import create_react_agent

    HAS_LLM_STACK = True
except ImportError:
    HumanMessage = None  # type: ignore[assignment]
    SystemMessage = None  # type: ignore[assignment]
    ChatOpenAI = None  # type: ignore[assignment]
    create_react_agent = None  # type: ignore[assignment]
    HAS_LLM_STACK = False

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
VALID_RUNTIME_MODES = {"agent", "offline-workflow", "hybrid"}

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


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


def bool_config(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


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


def sanitize_json_value(value: Any) -> Any:
    if isinstance(value, str):
        return value.encode("utf-8", errors="replace").decode("utf-8")
    if isinstance(value, dict):
        return {str(sanitize_json_value(key)): sanitize_json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_json_value(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_json_value(item) for item in value]
    return value


def get_ops_config(config: dict[str, Any]) -> dict[str, Any]:
    defaults = {
        "planning_enabled": True,
        "require_plan_confirmation": True,
        "auto_execute_after_plan": False,
        "audit_enabled": True,
        "audit_log_path": "../logs/tool_calls.jsonl",
        "continue_on_server_error": True,
    }
    defaults.update(config.get("ops_mode", {}) or {})
    return defaults


def normalize_runtime_mode(value: Any) -> str:
    normalized = str(value or "agent").strip().lower().replace("_", "-")
    aliases = {
        "agent": "agent",
        "llm": "agent",
        "ops-agent": "agent",
        "offline": "offline-workflow",
        "offline-workflow": "offline-workflow",
        "workflow": "offline-workflow",
        "diagnose": "offline-workflow",
        "hybrid": "hybrid",
    }
    if normalized not in aliases:
        raise ValueError(f"unsupported runtime mode: {value}")
    return aliases[normalized]


def get_runtime_config(config: dict[str, Any]) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "mode": "agent",
        "fallback_to_offline": True,
    }
    defaults.update(config.get("runtime", {}) or {})
    defaults["mode"] = normalize_runtime_mode(defaults.get("mode"))
    defaults["fallback_to_offline"] = bool_config(defaults.get("fallback_to_offline"), True)
    return defaults


def has_model_api_key(config: dict[str, Any]) -> bool:
    model_config = config.get("model", {}) or {}
    return bool(
        str(
            model_config.get("api_key")
            or os.environ.get("MINIMAX_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or ""
        ).strip()
    )


def llm_unavailable_reason(config: dict[str, Any]) -> str | None:
    if not has_model_api_key(config):
        return "缺少 MINIMAX_API_KEY 或 OPENAI_API_KEY。"
    if not HAS_LLM_STACK:
        return "当前环境未安装 langchain-openai/langgraph，无法启用 LLM Agent。"
    return None


def get_audit_log_path(ops_config: dict[str, Any]) -> Path | None:
    if not bool_config(ops_config.get("audit_enabled"), True):
        return None
    return resolve_path(str(ops_config.get("audit_log_path", "../logs/tool_calls.jsonl")))


def write_audit_event(audit_path: Path | None, event: dict[str, Any]) -> None:
    if audit_path is None:
        return
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_path.open("a", encoding="utf-8") as audit_file:
        audit_file.write(json.dumps(sanitize_json_value(event), ensure_ascii=False) + "\n")


def classify_command_risk(command: str) -> tuple[str, str]:
    normalized = f" {command.strip().lower()} "
    first_token = command.strip().split(maxsplit=1)[0].lower() if command.strip() else ""
    high_risk_tokens = (
        " rm ",
        " del ",
        " rmdir ",
        " format ",
        " shutdown",
        " reboot",
        " poweroff",
        " kill ",
        " taskkill",
        " stop-service",
        " restart-service",
        " systemctl restart",
        " systemctl stop",
        " docker rm",
        " docker stop",
        " kubectl delete",
        " >",
        ">>",
    )
    mutating_tokens = (
        " mv ",
        " move ",
        " cp ",
        " copy ",
        " mkdir ",
        " touch ",
        " setx ",
        " export ",
        " pip install",
        " uv pip install",
        " systemctl start",
        " docker restart",
        " kubectl apply",
    )
    read_only_prefixes = {
        "ls",
        "dir",
        "pwd",
        "whoami",
        "where",
        "which",
        "cat",
        "type",
        "more",
        "tail",
        "head",
        "grep",
        "findstr",
        "get-content",
        "select-string",
        "ps",
        "tasklist",
        "netstat",
        "npu-smi",
    }

    if not command.strip():
        return "invalid", "empty command"
    if any(token in normalized for token in high_risk_tokens):
        return "high", "may delete, stop, restart, kill processes, or modify files by redirection"
    if any(token in normalized for token in mutating_tokens):
        return "medium", "may change local state"
    if first_token in read_only_prefixes:
        return "low", "read-only diagnostic command"
    return "medium", "unknown command impact"


def classify_tool_call(tool_name: str, arguments: dict[str, Any]) -> tuple[str, str]:
    if tool_name == "run_shell_command":
        return classify_command_risk(str(arguments.get("command", "")))
    read_only_tools = {
        "read_local_file",
        "search_knowledge_base",
        "get_variable",
        "list_variables_by_prefix",
        "export_variables",
        "check_cann_env",
        "check_npu_status_info",
        "parse_mindie_log",
        "inspect_inference_health",
        "diagnose_ascend_inference_issue",
        "generate_ascend_remediation_plan",
        "verify_mindie_config",
        "suggest_mindie_config_patch",
        "restart_service_plan",
        "suggest_config_patch",
        "verify_service_recovery",
        "check_process",
        "check_port",
        "list_listening_ports",
        "check_disk_usage",
        "check_memory_usage",
        "check_env_vars",
        "tail_log",
        "grep_log",
        "detect_recent_errors",
        "inspect_service_health",
        "detect_serving_framework",
        "validate_model_path",
        "parse_serving_log",
        "verify_serving_config",
        "suggest_serving_config_patch",
        "check_accelerator_status",
        "check_accelerator_env",
        "detect_memory_pressure",
        "list_accelerator_processes",
        "list_containers",
        "inspect_container",
        "get_container_logs",
        "check_container_health",
        "check_container_ports",
        "check_container_mounts",
        "check_container_env",
        "restart_container_dry_run",
    }
    if tool_name in read_only_tools:
        return "low", "read-only tool"
    if tool_name in {"apply_mindie_config_patch", "apply_config_patch", "rollback_config"}:
        if bool_config(arguments.get("dry_run"), True):
            return "low", "dry-run config mutation preview"
        return "medium", "writes config after backup"
    if tool_name in {"apply_serving_config_patch"}:
        if bool_config(arguments.get("dry_run"), True):
            return "low", "dry-run AI serving config patch preview"
        return "medium", "writes AI serving config after backup"
    if tool_name in {"backup_mindie_config", "backup_config"}:
        return "medium", "writes a local backup file"
    if tool_name == "restart_service_with_approval":
        return "high", "may restart an inference service"
    if tool_name == "restart_container_with_approval":
        return "high", "may restart a container"
    if tool_name in {"save_variable", "refresh_knowledge_base"}:
        return "medium", "updates local Agent state"
    if tool_name == "delete_variable":
        return "medium", "deletes local Agent memory"
    return "medium", "unknown tool impact"


def requires_tool_approval(ops_config: dict[str, Any], risk_level: str) -> bool:
    if not bool_config(ops_config.get("tool_approval_enabled"), True):
        return False
    approval_levels = ops_config.get("tool_approval_risk_levels", ["medium", "high", "invalid"])
    return risk_level in set(approval_levels)


def request_tool_approval(
    session_name: str,
    tool_name: str,
    arguments: dict[str, Any],
    risk_level: str,
    risk_reason: str,
) -> bool:
    print(
        "\n[工具审批]\n"
        f"server: {session_name}\n"
        f"tool: {tool_name}\n"
        f"risk_level: {risk_level}\n"
        f"risk_reason: {risk_reason}\n"
        f"arguments: {json.dumps(arguments, ensure_ascii=False)}"
    )
    try:
        approval = input("是否允许调用该工具? [y/n] ").strip().lower()
    except EOFError:
        return False
    return approval == "y"


async def connect_server(
    session_name: str,
    server_config: dict[str, Any],
    stack: AsyncExitStack,
    ops_config: dict[str, Any],
    audit_path: Path | None,
) -> ConnectedServer:
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

    server_stack = AsyncExitStack()
    read_write = await server_stack.enter_async_context(stdio_client(server_params))
    read, write = read_write
    session = await server_stack.enter_async_context(ClientSession(read, write))
    try:
        await session.initialize()
    except Exception:
        await server_stack.aclose()
        raise

    tools_result = await session.list_tools()
    tools_source = getattr(tools_result, "tools", tools_result)
    tools: list[Any] = []

    for tool_spec in tools_source:
        tool_name = getattr(tool_spec, "name")
        tool_description = getattr(tool_spec, "description", "") or ""
        input_schema = getattr(tool_spec, "inputSchema", None) or getattr(tool_spec, "input_schema", None)
        args_model = build_args_model(tool_name, input_schema)

        async def call_mcp_tool(_tool_name: str = tool_name, **kwargs: Any) -> str:
            kwargs = sanitize_json_value(kwargs)
            started = time.perf_counter()
            risk_level, risk_reason = classify_tool_call(_tool_name, kwargs)
            event: dict[str, Any] = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "server": session_name,
                "tool": _tool_name,
                "arguments": kwargs,
                "risk_level": risk_level,
                "risk_reason": risk_reason,
            }

            if requires_tool_approval(ops_config, risk_level):
                approved = request_tool_approval(session_name, _tool_name, kwargs, risk_level, risk_reason)
                event["approved_by_human"] = approved
                if not approved:
                    text_result = (
                        "approved: false\n"
                        f"risk_level: {risk_level}\n"
                        f"risk_reason: {risk_reason}\n"
                        f"tool: {session_name}.{_tool_name}\n"
                        "result: Tool call rejected by human or non-interactive policy.\n"
                    )
                    event.update(
                        {
                            "success": False,
                            "skipped": True,
                            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                            "result_chars": len(text_result),
                        }
                    )
                    write_audit_event(audit_path, event)
                    return text_result

            try:
                result = await session.call_tool(_tool_name, arguments=kwargs)
                text_result = stringify_tool_result(result)
                event.update(
                    {
                        "success": True,
                        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                        "result_chars": len(text_result),
                    }
                )
                return text_result
            except Exception as exc:
                event.update(
                    {
                        "success": False,
                        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                        "error": str(exc),
                    }
                )
                raise
            finally:
                write_audit_event(audit_path, event)

        full_tool_name = f"{session_name}__{tool_name}"
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

    stack.push_async_callback(server_stack.aclose)
    return ConnectedServer(name=session_name, session=session, tools=tools)


async def build_connections(stack: AsyncExitStack) -> list[ConnectedServer]:
    config = load_config()
    ops_config = get_ops_config(config)
    audit_path = get_audit_log_path(ops_config)
    server_configs = [item for item in config.get("servers", []) if item.get("enabled", True)]

    connected_servers: list[ConnectedServer] = []
    for server_config in server_configs:
        try:
            connected_servers.append(
                await connect_server(server_config["name"], server_config, stack, ops_config, audit_path)
            )
        except Exception as exc:
            if not bool_config(ops_config.get("continue_on_server_error"), True) or bool_config(
                server_config.get("required"), False
            ):
                raise
            print(
                f"[警告] MCP Server 启动失败，已跳过: {server_config.get('name')} ({exc})",
                file=sys.stderr,
            )

    return connected_servers


def build_model(config: dict[str, Any]) -> Any:
    model_config = config.get("model", {})
    model_name = (
        model_config.get("model")
        or os.environ.get("MCP_CORE_HUB_MODEL")
        or "minimax-text-01"
    )
    base_url = (
        model_config.get("base_url")
        or os.environ.get("MINIMAX_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or "https://api.minimaxi.chat/v1"
    )
    api_key = (
        model_config.get("api_key")
        or os.environ.get("MINIMAX_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
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


def build_system_prompt(
    connected_servers: list[ConnectedServer],
    ops_config: dict[str, Any],
    runtime_mode: str = "agent",
) -> str:
    tool_names = [tool.name for server in connected_servers for tool in server.tools]
    if tool_names:
        tool_block = "\n".join(f"- {name}" for name in tool_names)
    else:
        tool_block = "- (no tools loaded)"

    planning_state = "开启" if bool_config(ops_config.get("planning_enabled"), True) else "关闭"
    auto_state = "开启" if bool_config(ops_config.get("auto_execute_after_plan"), False) else "关闭"

    return (
        "你是 mcp-core-hub 的 AI Infra Ops Agent，面向 AI 服务部署、推理运行、容器环境、GPU/NPU 资源和安全修复。\n"
        "用户给出运维问题后，你要像值班 SRE 一样先界定问题和风险边界，再自主选择 MCP 工具收集证据、形成根因假设和安全修复预案。\n"
        f"当前运行模式: {runtime_mode}; 计划模式: {planning_state}; 自动执行计划: {auto_state}。\n\n"
        "硬性流程：\n"
        "1. 先复述用户问题，并说明本轮不会绕过审批执行高风险动作。\n"
        "2. 优先收集四类只读证据：host(runtime-ops)、service(ai-serving-ops)、container(container-ops)、accelerator(accelerator-ops)。\n"
        "3. 再检索 runbook(search_knowledge_base) 和历史案例(list_variables_by_prefix prefix=incident:)。\n"
        "4. 需要修复配置时，先 verify，再 suggest patch，最后只允许 apply_*_config_patch(dry_run=true) 做预览。\n"
        "5. 不要直接执行重启、写配置、删除、kill 进程等高风险动作；如果确实需要，把它写入 Approval Required。Hub 会对 medium/high 工具弹出审批。\n"
        "6. 如果工具返回 approved:false 或 rejected，必须把动作标记为未执行，不得假装成功。\n"
        "7. 最终报告必须包含这些标题：Incident Summary、Evidence、Root Cause Hypothesis、Runbook References、Memory Matches、Safe Repair Preview、Approval Required、Verification Plan。\n"
        "8. 证据要尽量引用工具输出中的日志行号、HTTP 状态、端口、进程、容器 health/mount/port、GPU/NPU 显存和进程、配置键、runbook 片段或历史 incident key。\n\n"
        "当前可用工具如下。工具名使用 LLM API 兼容格式 server__tool：\n"
        f"{tool_block}"
    )


def build_agent_user_prompt(user_input: str, confirmed_plan: str | None = None) -> str:
    plan_block = f"\n\n已由用户确认的运维计划：\n{confirmed_plan}\n" if confirmed_plan else ""
    return (
        f"用户问题：\n{user_input.strip()}\n"
        f"{plan_block}\n"
        "请按系统规则完成本轮运维处理。不要要求用户手动输入 call 命令；你需要自主选择合适 MCP 工具。"
    )


async def generate_ops_plan(
    model: Any,
    system_prompt: str,
    user_input: str,
    tool_names: list[str],
) -> str:
    if SystemMessage is None:
        raise RuntimeError("当前环境未安装 langchain-core，无法生成运维计划。")

    tool_block = "\n".join(f"- {name}" for name in tool_names) if tool_names else "- (no tools loaded)"
    planner_prompt = (
        f"{system_prompt}\n\n"
        "现在只生成计划，不要调用工具。\n"
        "请像一个值班 SRE 一样，先判断问题类型，再给出一份可执行的运维处理计划。\n"
        "计划需要包含：\n"
        "1. 问题理解\n"
        "2. 只读诊断步骤\n"
        "3. 可能的修复动作\n"
        "4. 风险分级与需要人工确认的动作\n"
        "5. 预期产出\n\n"
        "可用工具：\n"
        f"{tool_block}"
    )
    response = await model.ainvoke(
        [
            SystemMessage(content=planner_prompt),
            HumanMessage(content=user_input),
        ]
    )
    return str(getattr(response, "content", response)).strip()


def handle_runtime_command(
    user_input: str,
    ops_config: dict[str, Any],
    runtime_config: dict[str, Any],
) -> bool:
    if user_input == "/ops":
        print(
            "Ops 模式: "
            f"runtime_mode={runtime_config.get('mode')}, "
            f"fallback_to_offline={bool_config(runtime_config.get('fallback_to_offline'), True)}, "
            f"planning_enabled={bool_config(ops_config.get('planning_enabled'), True)}, "
            f"auto_execute_after_plan={bool_config(ops_config.get('auto_execute_after_plan'), False)}, "
            f"require_plan_confirmation={bool_config(ops_config.get('require_plan_confirmation'), True)}"
        )
        return True

    if user_input == "/mode" or user_input.startswith("/mode "):
        parts = user_input.split(maxsplit=1)
        if len(parts) == 1:
            print(f"当前 runtime mode: {runtime_config.get('mode')}")
            print("可选模式: agent | offline | hybrid")
            return True
        try:
            runtime_config["mode"] = normalize_runtime_mode(parts[1])
        except ValueError as exc:
            print(f"模式切换失败: {exc}. 可选模式: agent | offline | hybrid")
            return True
        print(f"runtime mode 已切换为 {runtime_config['mode']}。")
        return True

    if user_input in {"/plan on", "/plan off"}:
        ops_config["planning_enabled"] = user_input.endswith("on")
        print(f"计划模式已{'开启' if ops_config['planning_enabled'] else '关闭'}。")
        return True

    if user_input in {"/auto on", "/auto off"}:
        ops_config["auto_execute_after_plan"] = user_input.endswith("on")
        print(f"计划后自动执行已{'开启' if ops_config['auto_execute_after_plan'] else '关闭'}。")
        return True

    return False


def workflow_args_from_natural_input(user_input: str) -> dict[str, Any]:
    return {"symptom": sanitize_json_value(user_input.strip())}


def parse_diagnose_command(user_input: str) -> dict[str, Any] | None:
    prefixes = ("/diagnose ", "diagnose ")
    matched_prefix = next((prefix for prefix in prefixes if user_input.startswith(prefix)), None)
    if matched_prefix is None:
        return None

    payload = user_input[len(matched_prefix):].strip()
    if not payload:
        return {"error": "missing symptom"}

    if payload.startswith("{"):
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            return {"error": f"invalid JSON payload: {exc}"}
        if not isinstance(data, dict):
            return {"error": "diagnose JSON payload must be an object"}
        if not str(data.get("symptom", "")).strip():
            return {"error": "diagnose JSON payload requires symptom"}
        return sanitize_json_value(data)

    return {"symptom": sanitize_json_value(payload)}


async def invoke_workflow_tool(tool_map: dict[str, Any], tool_name: str, arguments: dict[str, Any]) -> str:
    tool = tool_map.get(tool_name)
    if tool is None:
        return f"[missing tool] {tool_name}"
    return await tool.ainvoke(sanitize_json_value(arguments))


def compact_json_result(text: str, max_chars: int = 2200) -> str:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text if len(text) <= max_chars else text[:max_chars] + "\n...<truncated>"

    if isinstance(data, dict):
        env_snapshot = data.get("env_snapshot")
        if isinstance(env_snapshot, dict):
            path_value = str(env_snapshot.get("PATH", ""))
            if len(path_value) > 240:
                env_snapshot["PATH"] = path_value[:240] + "...<truncated>"
    rendered = json.dumps(data, ensure_ascii=False, indent=2)
    return rendered if len(rendered) <= max_chars else rendered[:max_chars] + "\n...<truncated>"


def extract_patch_json(suggestion_text: str) -> str:
    try:
        suggestion = json.loads(suggestion_text)
    except json.JSONDecodeError:
        return "{}"
    patch = suggestion.get("patch", {}) if isinstance(suggestion, dict) else {}
    if not isinstance(patch, dict):
        return "{}"
    return json.dumps(patch, ensure_ascii=False)


def build_tool_map(connected_servers: list[ConnectedServer]) -> dict[str, Any]:
    tool_map: dict[str, Any] = {}
    for server in connected_servers:
        safe_prefix = f"{server.name}__"
        for tool in server.tools:
            tool_map[tool.name] = tool
            if tool.name.startswith(safe_prefix):
                raw_tool_name = tool.name[len(safe_prefix):]
                tool_map[f"{server.name}.{raw_tool_name}"] = tool
    return tool_map


def should_use_legacy_ascend_workflow(workflow_args: dict[str, Any]) -> bool:
    text = " ".join(str(value).lower() for value in workflow_args.values())
    return any(token in text for token in ["mindie", "ascend", "cann", "npu", "昇腾"]) and not any(
        token in text for token in ["vllm", "docker", "container", "gpu", "cuda"]
    )


def infer_ai_infra_defaults(workflow_args: dict[str, Any]) -> dict[str, Any]:
    inferred = dict(workflow_args)
    symptom = str(inferred.get("symptom", "")).lower()
    if "service_url" not in inferred and any(token in symptom for token in ["vllm", "503", "/v1/models"]):
        inferred["service_url"] = "http://127.0.0.1:8000/v1/models"
    if "log_path" not in inferred and any(token in symptom for token in ["vllm", "503", "model", "模型"]):
        inferred["log_path"] = "demo/vllm_503_gpu_memory.log"
    if "config_path" not in inferred and any(token in symptom for token in ["vllm", "503", "model", "模型"]):
        inferred["config_path"] = "demo/vllm_config.json"
    if "model_path" not in inferred and any(token in symptom for token in ["vllm", "qwen", "model", "模型"]):
        inferred["model_path"] = "demo/models/Qwen2.5-7B-Instruct"
    if "container" not in inferred and any(token in symptom for token in ["vllm", "container", "容器", "docker"]):
        inferred["container"] = "vllm-qwen"
    inferred.setdefault("accelerator_provider", "auto")
    inferred.setdefault("framework", "vllm" if "vllm" in symptom else "generic")
    return inferred


def extract_port_from_url(url: str) -> int | None:
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.port:
        return parsed.port
    if parsed.scheme == "http":
        return 80
    if parsed.scheme == "https":
        return 443
    return None


async def run_legacy_ascend_ops_workflow(tool_map: dict[str, Any], workflow_args: dict[str, Any]) -> str:
    if "error" in workflow_args:
        return f"确定性诊断命令解析失败: {workflow_args['error']}"

    symptom = str(workflow_args["symptom"]).strip()
    log_path = str(workflow_args.get("log_path", "")).strip()
    config_path = str(workflow_args.get("config_path", "")).strip()
    health_url = str(workflow_args.get("health_url", "")).strip()
    service_name = str(workflow_args.get("service_name", "mindie-llm")).strip() or "mindie-llm"

    plan = await invoke_workflow_tool(
        tool_map,
        "mcp-server-ascend-ops.generate_ascend_remediation_plan",
        {"symptom": symptom, "log_path": log_path},
    )
    diagnosis = await invoke_workflow_tool(
        tool_map,
        "mcp-server-ascend-ops.diagnose_ascend_inference_issue",
        {"symptom": symptom, "log_path": log_path},
    )
    runbook = await invoke_workflow_tool(
        tool_map,
        "mcp-server-rag-docs.search_knowledge_base",
        {"query": f"{symptom} MindIE CANN NPU HBM model load timeout 503"},
    )
    memory = await invoke_workflow_tool(
        tool_map,
        "mcp-server-memory-kv.list_variables_by_prefix",
        {"prefix": "incident:"},
    )
    config_check = await invoke_workflow_tool(
        tool_map,
        "mcp-server-ascend-ops.verify_mindie_config",
        {"config_path": config_path},
    )
    patch_suggestion = await invoke_workflow_tool(
        tool_map,
        "mcp-server-ascend-ops.suggest_mindie_config_patch",
        {"config_path": config_path, "symptom": symptom},
    )
    patch_preview = await invoke_workflow_tool(
        tool_map,
        "mcp-server-ascend-ops.apply_mindie_config_patch",
        {
            "config_path": config_path,
            "patch_json": extract_patch_json(patch_suggestion),
            "dry_run": True,
        },
    )
    restart_plan = await invoke_workflow_tool(
        tool_map,
        "mcp-server-ascend-ops.restart_service_plan",
        {"service_name": service_name},
    )

    health_check = ""
    if health_url:
        health_check = await invoke_workflow_tool(
            tool_map,
            "mcp-server-ascend-ops.inspect_inference_health",
            {"url": health_url},
        )

    sections = [
        "# Deterministic Ops Workflow Report",
        "",
        "LLM API Key: not required. This workflow called MCP tools in a fixed order.",
        f"Symptom: {symptom}",
        "",
        "## 1. Plan",
        plan,
        "",
        "## 2. Evidence Diagnosis",
        compact_json_result(diagnosis),
        "",
        "## 3. Runbook Retrieval",
        runbook,
        "",
        "## 4. Historical Memory",
        compact_json_result(memory),
        "",
        "## 5. Config Verification",
        compact_json_result(config_check),
        "",
        "## 6. Safe Repair Patch Preview",
        compact_json_result(patch_suggestion),
        "",
        "## 7. Dry-run Apply Result",
        compact_json_result(patch_preview),
        "",
        "## 8. Restart Plan",
        compact_json_result(restart_plan),
    ]
    if health_check:
        sections.extend(["", "## 9. Health Check", compact_json_result(health_check)])

    return "\n".join(sections)


async def run_ai_infra_ops_workflow(tool_map: dict[str, Any], workflow_args: dict[str, Any]) -> str:
    if "error" in workflow_args:
        return f"确定性诊断命令解析失败: {workflow_args['error']}"

    args = infer_ai_infra_defaults(workflow_args)
    symptom = str(args["symptom"]).strip()
    service_url = str(args.get("service_url", args.get("health_url", ""))).strip()
    log_path = str(args.get("log_path", "")).strip()
    config_path = str(args.get("config_path", "")).strip()
    model_path = str(args.get("model_path", "")).strip()
    container = str(args.get("container", "")).strip()
    provider = str(args.get("accelerator_provider", "auto")).strip() or "auto"
    framework = str(args.get("framework", "generic")).strip() or "generic"
    port = args.get("port")
    if port is None:
        port = extract_port_from_url(service_url)

    host_checks: list[str] = []
    if port:
        host_checks.append(await invoke_workflow_tool(tool_map, "mcp-server-runtime-ops.check_port", {"port": int(port)}))
    host_checks.append(await invoke_workflow_tool(tool_map, "mcp-server-runtime-ops.check_process", {"keyword": framework if framework != "generic" else "python"}))
    host_checks.append(await invoke_workflow_tool(tool_map, "mcp-server-runtime-ops.check_disk_usage", {"path": "."}))
    host_checks.append(await invoke_workflow_tool(tool_map, "mcp-server-runtime-ops.check_memory_usage", {}))
    if log_path:
        host_checks.append(await invoke_workflow_tool(tool_map, "mcp-server-runtime-ops.detect_recent_errors", {"log_path": log_path}))

    container_checks: list[str] = []
    if container:
        for tool_name, arguments in [
            ("mcp-server-container-ops.inspect_container", {"container": container}),
            ("mcp-server-container-ops.get_container_logs", {"container": container, "lines": 100}),
            ("mcp-server-container-ops.check_container_health", {"container": container}),
            ("mcp-server-container-ops.check_container_ports", {"container": container}),
            ("mcp-server-container-ops.check_container_mounts", {"container": container}),
            ("mcp-server-container-ops.check_container_env", {"container": container, "names": ["MODEL_PATH", "GPU_MEMORY_UTILIZATION", "MAX_MODEL_LEN"]}),
        ]:
            container_checks.append(await invoke_workflow_tool(tool_map, tool_name, arguments))
    else:
        container_checks.append(await invoke_workflow_tool(tool_map, "mcp-server-container-ops.list_containers", {}))

    service_checks: list[str] = []
    if service_url:
        service_checks.append(await invoke_workflow_tool(tool_map, "mcp-server-ai-serving-ops.inspect_service_health", {"url": service_url}))
    service_checks.append(await invoke_workflow_tool(tool_map, "mcp-server-ai-serving-ops.detect_serving_framework", {"url": service_url, "log_path": log_path}))
    if log_path:
        service_checks.append(await invoke_workflow_tool(tool_map, "mcp-server-ai-serving-ops.parse_serving_log", {"log_path": log_path, "framework": framework}))
    if model_path:
        service_checks.append(await invoke_workflow_tool(tool_map, "mcp-server-ai-serving-ops.validate_model_path", {"model_path": model_path}))
    if config_path:
        service_checks.append(await invoke_workflow_tool(tool_map, "mcp-server-ai-serving-ops.verify_serving_config", {"config_path": config_path, "framework": framework}))
        patch_suggestion = await invoke_workflow_tool(
            tool_map,
            "mcp-server-ai-serving-ops.suggest_serving_config_patch",
            {"config_path": config_path, "symptom": symptom, "framework": framework},
        )
        service_checks.append(patch_suggestion)
        service_checks.append(
            await invoke_workflow_tool(
                tool_map,
                "mcp-server-ai-serving-ops.apply_serving_config_patch",
                {
                    "config_path": config_path,
                    "patch_json": extract_patch_json(patch_suggestion),
                    "dry_run": True,
                },
            )
        )

    accelerator_checks = [
        await invoke_workflow_tool(tool_map, "mcp-server-accelerator-ops.check_accelerator_status", {"provider": provider}),
        await invoke_workflow_tool(tool_map, "mcp-server-accelerator-ops.check_accelerator_env", {"provider": provider}),
        await invoke_workflow_tool(tool_map, "mcp-server-accelerator-ops.detect_memory_pressure", {"provider": provider}),
        await invoke_workflow_tool(tool_map, "mcp-server-accelerator-ops.list_accelerator_processes", {"provider": provider}),
    ]

    runbook = await invoke_workflow_tool(
        tool_map,
        "mcp-server-rag-docs.search_knowledge_base",
        {"query": f"{symptom} AI serving vLLM 503 CUDA out of memory model load timeout docker volume GPU NPU"},
    )
    memory = await invoke_workflow_tool(
        tool_map,
        "mcp-server-memory-kv.list_variables_by_prefix",
        {"prefix": "incident:"},
    )

    restart_plan = ""
    if container:
        restart_plan = await invoke_workflow_tool(
            tool_map,
            "mcp-server-container-ops.restart_container_dry_run",
            {"container": container},
        )

    sections = [
        "# AI Infra Ops Workflow Report",
        "",
        "LLM API Key: not required. This workflow called MCP tools in a fixed AI Infra order.",
        "",
        "## Incident Summary",
        f"- Symptom: {symptom}",
        f"- Service URL: {service_url or 'not provided'}",
        f"- Container: {container or 'not provided'}",
        f"- Framework: {framework}",
        f"- Accelerator provider: {provider}",
        "",
        "## Plan",
        "1. Collect read-only host, service, container, and accelerator evidence.",
        "2. Retrieve runbooks and historical incidents.",
        "3. Preview safe config repair with dry-run only.",
        "4. Mark config writes and restarts as approval-required actions.",
        "5. Verify recovery after any approved repair.",
        "",
        "## Evidence",
        "### Host Runtime",
        "\n\n".join(compact_json_result(item) for item in host_checks),
        "",
        "### Container",
        "\n\n".join(compact_json_result(item) for item in container_checks),
        "",
        "### AI Serving",
        "\n\n".join(compact_json_result(item) for item in service_checks),
        "",
        "### Accelerator",
        "\n\n".join(compact_json_result(item) for item in accelerator_checks),
        "",
        "## Root Cause Hypothesis",
        "The leading hypothesis is model worker startup failure caused by accelerator memory pressure or aggressive serving config. "
        "If model_path or container mounts are missing, prioritize volume/path repair before memory tuning.",
        "",
        "## Runbook References",
        runbook,
        "",
        "## Memory Matches",
        compact_json_result(memory),
        "",
        "## Safe Repair Preview",
        "Config patch preview above is dry-run only. Suggested fixes may include lowering gpu_memory_utilization/max_model_len or correcting model path/mounts.",
        "",
        "## Approval Required",
        "- apply_serving_config_patch(dry_run=false): medium risk, writes config after backup.",
        "- restart_container_with_approval(execute=true): high risk, restarts the serving container.",
        "",
        "## Executed Actions",
        "No write, rollback, kill, or restart action was executed by the deterministic workflow.",
        "",
        "## Verification Result",
        "Not executed yet. After approved repair, call verify_service_recovery and re-check container logs/health.",
        "",
        "## Memory Writeback",
        "Not written automatically in offline workflow. After confirmation, save incident summary under incident:<service-or-container>:<date>.",
    ]
    if restart_plan:
        sections.extend(["", "### Restart Dry-run Plan", compact_json_result(restart_plan)])

    return "\n".join(sections)


async def run_deterministic_ops_workflow(tool_map: dict[str, Any], workflow_args: dict[str, Any]) -> str:
    if should_use_legacy_ascend_workflow(workflow_args):
        return await run_legacy_ascend_ops_workflow(tool_map, workflow_args)
    return await run_ai_infra_ops_workflow(tool_map, workflow_args)


async def run_hybrid_ops_workflow(
    model: Any,
    system_prompt: str,
    tool_map: dict[str, Any],
    workflow_args: dict[str, Any],
) -> str:
    if SystemMessage is None or HumanMessage is None:
        raise RuntimeError("当前环境未安装 langchain-core，无法启用 hybrid 模式。")

    deterministic_report = await run_deterministic_ops_workflow(tool_map, workflow_args)
    symptom = str(workflow_args.get("symptom", "")).strip()
    hybrid_prompt = (
        f"{system_prompt}\n\n"
        "你现在处于 hybrid 模式。确定性 workflow 已经完成工具调用，你不能再调用工具，也不要声称执行了新的动作。\n"
        "请基于下方 workflow report 生成一份更自然、适合面试展示和真实值班交接的运维报告。\n"
        "报告必须保留这些标题：Incident Summary、Evidence、Root Cause Hypothesis、Runbook References、Memory Matches、Safe Repair Preview、Approval Required、Verification Plan。"
    )
    response = await model.ainvoke(
        [
            SystemMessage(content=hybrid_prompt),
            HumanMessage(
                content=(
                    f"用户问题：{symptom}\n\n"
                    f"Deterministic workflow report:\n{deterministic_report}"
                )
            ),
        ]
    )
    llm_summary = str(getattr(response, "content", response)).strip()
    return "\n\n".join(
        [
            deterministic_report,
            "# LLM Ops Agent Hybrid Summary",
            llm_summary,
        ]
    )


async def run_repl() -> None:
    config = load_config()
    ops_config = get_ops_config(config)
    runtime_config = get_runtime_config(config)

    async with AsyncExitStack() as stack:
        connected_servers = await build_connections(stack)
        all_tools = [tool for server in connected_servers for tool in server.tools]
        tool_map = build_tool_map(connected_servers)
        system_prompt = build_system_prompt(connected_servers, ops_config, runtime_config["mode"])

        model_error: str | None = None
        model: Any = None
        agent: Any = None
        agent_signature: tuple[Any, ...] | None = None
        messages: list[Any] = []

        async def ensure_llm_ready() -> bool:
            nonlocal agent, agent_signature, model, model_error, system_prompt
            unavailable = llm_unavailable_reason(config)
            if unavailable:
                model_error = unavailable
                return False

            try:
                if model is None:
                    model = build_model(config)
            except Exception as exc:
                model_error = str(exc)
                return False

            if create_react_agent is None:
                model_error = "当前环境未安装 langgraph，无法创建 ReAct Agent。"
                return False

            system_prompt = build_system_prompt(connected_servers, ops_config, runtime_config["mode"])
            signature = (
                runtime_config["mode"],
                bool_config(ops_config.get("planning_enabled"), True),
                bool_config(ops_config.get("auto_execute_after_plan"), False),
                bool_config(ops_config.get("require_plan_confirmation"), True),
            )
            if agent is None or agent_signature != signature:
                agent = create_react_agent(model, tools=all_tools, prompt=system_prompt)
                agent_signature = signature
            return True

        async def fallback_or_stop() -> bool:
            nonlocal system_prompt
            if bool_config(runtime_config.get("fallback_to_offline"), True):
                print(f"[提示] 自动降级为 offline-workflow：{model_error}")
                runtime_config["mode"] = "offline-workflow"
                system_prompt = build_system_prompt(connected_servers, ops_config, runtime_config["mode"])
                return True
            print(f"[错误] 无法启用 {runtime_config['mode']}：{model_error}")
            return False

        if runtime_config["mode"] in {"agent", "hybrid"} and not await ensure_llm_ready():
            if not await fallback_or_stop():
                return

        print(f"mcp-core-hub 已启动（runtime mode: {runtime_config['mode']}）。输入问题，或输入 exit/quit 退出。")
        print("运行时命令: /mode agent|offline|hybrid | /ops | /diagnose <symptom|json> | /plan on|off | /auto on|off | tools")
        if runtime_config["mode"] == "offline-workflow":
            print("离线模式下普通自然语言输入会自动进入确定性 Ops Workflow；不需要 LLM API Key。")

        while True:
            user_input = str(sanitize_json_value(input("mcp-core-hub> ").strip()))
            if user_input.lower() in {"exit", "quit"}:
                break
            if not user_input:
                continue

            if handle_runtime_command(user_input, ops_config, runtime_config):
                messages = []
                if runtime_config["mode"] in {"agent", "hybrid"} and not await ensure_llm_ready():
                    if not await fallback_or_stop():
                        break
                else:
                    system_prompt = build_system_prompt(connected_servers, ops_config, runtime_config["mode"])
                continue

            if user_input == "tools":
                for tool_name in sorted({tool.name for tool in all_tools}):
                    print(tool_name)
                print("提示: call 命令也兼容旧式别名 server.tool。")
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

            workflow_args = parse_diagnose_command(user_input)
            if workflow_args is not None:
                print(await run_deterministic_ops_workflow(tool_map, workflow_args))
                continue

            if runtime_config["mode"] == "offline-workflow":
                print(await run_deterministic_ops_workflow(tool_map, workflow_args_from_natural_input(user_input)))
                continue

            if runtime_config["mode"] == "hybrid":
                if not await ensure_llm_ready():
                    if await fallback_or_stop():
                        print(await run_deterministic_ops_workflow(tool_map, workflow_args_from_natural_input(user_input)))
                    continue
                print(
                    await run_hybrid_ops_workflow(
                        model,
                        system_prompt,
                        tool_map,
                        workflow_args_from_natural_input(user_input),
                    )
                )
                continue

            if runtime_config["mode"] == "agent":
                if not await ensure_llm_ready():
                    if await fallback_or_stop():
                        print(await run_deterministic_ops_workflow(tool_map, workflow_args_from_natural_input(user_input)))
                    continue

                confirmed_plan: str | None = None
                if bool_config(ops_config.get("planning_enabled"), True):
                    plan = await generate_ops_plan(
                        model,
                        system_prompt,
                        user_input,
                        [tool.name for tool in all_tools],
                    )
                    print("\n[运维计划]\n" + plan + "\n")
                    should_execute = bool_config(ops_config.get("auto_execute_after_plan"), False)
                    if not should_execute and bool_config(ops_config.get("require_plan_confirmation"), True):
                        approval = input("是否按该计划继续调用工具执行? [y/n] ").strip().lower()
                        should_execute = approval == "y"
                    if not should_execute:
                        print("已停在计划阶段，未调用工具。")
                        continue
                    confirmed_plan = plan

                messages.append(HumanMessage(content=build_agent_user_prompt(user_input, confirmed_plan)))
                result = await agent.ainvoke({"messages": messages})
                messages = result["messages"]
                last_message = messages[-1]
                print(getattr(last_message, "content", str(last_message)))
                continue

            print("未知 runtime mode，输入 /mode agent、/mode offline 或 /mode hybrid。")


if __name__ == "__main__":
    asyncio.run(run_repl())
