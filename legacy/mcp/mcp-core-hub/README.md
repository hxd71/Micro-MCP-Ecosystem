# mcp-core-hub

> Deprecated compatibility/demo component. The production entrypoints are `aiops-agent` and `aiops`; this Hub is not part of the Local AI Ops Agent v1 runtime.

`mcp-core-hub` 是项目里的运维 Agent 中枢。它不只是把多个 MCP Server 的工具交给模型，而是负责把用户描述的运维问题组织成“先计划、再执行、再总结”的处理流程。

核心职责是：

- 读取 `config.json`
- 启动并连接多个 MCP Server
- 把这些 Server 暴露出来的 Tools 交给 LangGraph
- 进入交互循环，持续接收用户输入
- 在计划模式下先生成运维处理计划，再由用户确认是否继续执行
- 支持 `diagnose <symptom|json>` 确定性工作流，不调用 LLM API
- 支持 `offline-workflow`、`agent`、`hybrid` 三种 runtime 模式
- 调用工具时写入 JSONL 审计日志
- 单个非必需插件启动失败时继续运行，避免一个插件拖垮整个 Hub

## 当前定位

这个 hub 面向 AI Infra 运维诊断场景，默认接入：

- `mcp-server-devops`：本地命令与文件读取工具
- `mcp-server-runtime-ops`：进程、端口、磁盘、内存、环境变量和日志检查
- `mcp-server-ai-serving-ops`：AI 服务健康、日志、模型路径、配置校验和安全补丁 dry-run
- `mcp-server-accelerator-ops`：NVIDIA GPU / Ascend NPU 状态、显存/HBM 和进程检查
- `mcp-server-container-ops`：Docker 容器状态、日志、端口、挂载和环境变量检查
- `mcp-server-ascend-ops`：CANN、NPU、MindIE 专业排障能力，作为 Ascend provider 兼容保留
- `mcp-server-rag-docs`：本地排障文档检索
- `mcp-server-memory-kv`：历史处理经验和状态记忆

用户可以直接输入类似“vLLM 容器启动后 503”“模型加载超时”“Docker volume 未挂载”“GPU/NPU 显存不足”“日志里出现 CANN 初始化失败”这样的运维现象。Hub 会先让模型输出处理计划；用户确认后，Agent 再通过 MCP 工具读取日志、执行只读检查、检索知识库，并在需要修改配置或重启服务前触发 Hub 审批。

## Runtime 模式

`config.json` 默认启用 LLM Agent，并在无 Key 时自动降级：

```json
{
  "runtime": {
    "mode": "agent",
    "fallback_to_offline": true
  }
}
```

- `offline-workflow`：确定性 `diagnose` 工作流，不需要 API Key；普通自然语言输入也会被当作故障现象进入固定诊断链路。
- `agent`：LLM + LangGraph ReAct，模型会基于自然语言自主选择 MCP tools，需要 `MINIMAX_API_KEY` 或 `OPENAI_API_KEY`。
- `hybrid`：先运行确定性 workflow 收集证据，再把报告交给 LLM 总结，需要 API Key。

运行时可输入：

```text
/mode agent
/mode offline
/mode hybrid
```

LLM 可见的工具名使用 OpenAI-compatible 友好的 `server__tool` 形式，例如 `mcp-server-ai-serving-ops__parse_serving_log`；手工 `call` 和确定性 workflow 仍兼容旧式 `server.tool` 别名。

## Ops 计划模式

`config.json` 中的 `ops_mode` 控制运维工作流：

```json
{
  "ops_mode": {
    "planning_enabled": true,
    "require_plan_confirmation": true,
    "auto_execute_after_plan": false,
    "tool_approval_enabled": true,
    "tool_approval_risk_levels": ["medium", "high", "invalid"],
    "audit_enabled": true,
    "audit_log_path": "../logs/tool_calls.jsonl",
    "continue_on_server_error": true
  }
}
```

含义：

- `planning_enabled`：是否先生成运维计划。
- `require_plan_confirmation`：计划生成后是否要求用户确认。
- `auto_execute_after_plan`：是否在计划生成后直接执行，适合演示环境，不建议生产默认开启。
- `tool_approval_enabled`：是否由 Hub 在调用中高风险工具前请求确认。
- `tool_approval_risk_levels`：哪些风险等级需要审批。
- `audit_enabled`：是否记录工具调用审计日志。
- `audit_log_path`：审计日志路径。
- `continue_on_server_error`：某个非必需 MCP Server 启动失败时是否跳过。

运行时可输入：

```text
/ops
/mode agent
/mode offline
/mode hybrid
/diagnose vLLM 容器 vllm-qwen 启动后 /v1/models 一直返回 503
/diagnose {"symptom":"vLLM service returns 503","service_url":"http://127.0.0.1:8000/v1/models","log_path":"demo/vllm_503_gpu_memory.log","config_path":"demo/vllm_config.json","container":"vllm-qwen","framework":"vllm"}
/plan on
/plan off
/auto on
/auto off
```

推荐默认策略是：计划模式开启，自动执行关闭。这样 Agent 会先像 Codex 计划模式一样把操作思路讲清楚，再由用户决定是否让它继续调用工具。

## API Key 边界

`diagnose ...` 确定性工作流不需要 API Key。它会按固定顺序调用 MCP 工具：

1. 收集 host runtime 证据：进程、端口、磁盘、内存、日志。
2. 收集 container 证据：health、logs、ports、mounts、env。
3. 收集 AI serving 证据：HTTP health、framework、模型路径、配置和日志。
4. 收集 accelerator 证据：GPU/NPU 显存/HBM、温度、进程。
5. 检索本地 runbook 和历史 incident memory。
6. 生成配置补丁建议并执行 dry-run。
7. 生成重启计划，但不执行重启。

直接输入自然语言时，行为取决于 runtime mode：

- `offline-workflow`：不需要 API Key，自动运行确定性 workflow。
- `agent`：需要 API Key，模型自主规划和调用 tools。
- `hybrid`：需要 API Key，先跑 workflow，再让模型总结。

如果当前 mode 是 `agent` 或 `hybrid` 但没有 Key，且 `fallback_to_offline=true`，Hub 会提示原因并降级到 `offline-workflow`。

## 运行要求

- Python 3.12+
- 需要一个支持原生 tool calling 的 Chat 模型
- 默认使用 OpenAI-compatible 模型接口

## 安装

在 `mcp-core-hub/` 目录下执行：

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
pip install -e .
```

## 启动

先设置 API Key（推荐使用环境变量，不要写进仓库）：

```powershell
$env:MINIMAX_API_KEY = "your_real_api_key"
```

可选：设置网关地址（仅在你需要切换网关时）：

```powershell
$env:MINIMAX_BASE_URL = "https://api.minimaxi.chat/v1"
```

Windows CMD：

```cmd
set MINIMAX_API_KEY=your_real_api_key
set MINIMAX_BASE_URL=https://api.minimaxi.chat/v1
```

Linux / macOS：

```bash
export MINIMAX_API_KEY="your_real_api_key"
export MINIMAX_BASE_URL="https://api.minimaxi.chat/v1"
```

```bash
python hub.py
```

仓库根目录也提供两个演示脚本：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\demo_ai_service_offline.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\demo_offline.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\demo_agent.ps1
```

`demo_ai_service_offline.ps1` 和 `demo_offline.ps1` 不需要 Key；`demo_agent.ps1` 只负责启动 Hub，不内置 Key。

如果你要用别的模型，修改 `config.json` 里的 `model` 段，或直接调整 `hub.py` 的 `build_model()`。

## 配置说明

`config.json` 里的 `servers` 数组用于声明要连接哪些 MCP Server：

- `name`：服务器名，工具会自动带上这个前缀
- `script`：MCP Server 的入口脚本路径
- `args`：启动参数
- `enabled`：是否启用

`model` 段用于配置模型：

- `provider`：当前示例为 `openai`
- `model`：模型名称
- `base_url`：OpenAI-compatible 网关地址
- `temperature`：采样温度
- `api_key`：可选，不建议写入仓库

示例：

```json
{
	"model": {
		"provider": "openai",
		"model": "minimax-text-01",
		"base_url": "https://api.minimaxi.chat/v1",
		"temperature": 0
	},
	"runtime": {
		"mode": "agent",
		"fallback_to_offline": true
	},
	"ops_mode": {
		"planning_enabled": true,
		"require_plan_confirmation": true,
		"auto_execute_after_plan": false,
		"tool_approval_enabled": true,
		"tool_approval_risk_levels": ["medium", "high", "invalid"],
		"audit_enabled": true,
		"audit_log_path": "../logs/tool_calls.jsonl",
		"continue_on_server_error": true
	},
	"servers": [
		{
			"name": "mcp-server-devops",
			"enabled": true,
			"script": "../server.py",
			"args": ["--transport", "stdio"],
			"env": {
				"MCP_APPROVAL_MODE": "auto",
				"MCP_ALLOW_AUTO_HIGH_RISK": "true"
			}
		},
		{
			"name": "mcp-server-ascend-ops",
			"enabled": true,
			"script": "../mcp-server-ascend-ops/server.py",
			"args": ["--transport", "stdio"],
			"required": true
		},
		{
			"name": "mcp-server-runtime-ops",
			"enabled": true,
			"script": "../mcp-server-runtime-ops/server.py",
			"args": ["--transport", "stdio"]
		},
		{
			"name": "mcp-server-ai-serving-ops",
			"enabled": true,
			"script": "../mcp-server-ai-serving-ops/server.py",
			"args": ["--transport", "stdio"]
		},
		{
			"name": "mcp-server-accelerator-ops",
			"enabled": true,
			"script": "../mcp-server-accelerator-ops/server.py",
			"args": ["--transport", "stdio"]
		},
		{
			"name": "mcp-server-container-ops",
			"enabled": true,
			"script": "../mcp-server-container-ops/server.py",
			"args": ["--transport", "stdio"]
		}
	]
}
```

这里的 `MCP_APPROVAL_MODE=auto` 只表示子 MCP Server 不在 stdio 协议通道里阻塞等待输入；真正的权限确认在 Hub 层完成。这样可以避免子进程 `input()` 干扰 MCP JSON-RPC 消息。

API Key 读取规则（按代码实际行为）：

1. 优先读取 `config.json` 里的 `model.api_key`
2. 若未配置，再读取环境变量 `MINIMAX_API_KEY`
3. 再次回退到 `OPENAI_API_KEY`

建议：

- 公共仓库不要写 `model.api_key`
- 仅在本机终端通过环境变量设置 Key
- 若怀疑泄露，立即轮换密钥

注意：当前 `base_url` 和 `model` 如果已在 `config.json` 中配置，将优先使用配置文件值；环境变量用于缺省回退。

## GitHub 发布建议

- 提交前检查 `git diff`，确认没有真实密钥或私有地址
- 不要提交 `.env`、本地日志、数据库快照等敏感文件
- 在 README 中仅保留 `your_real_api_key` 这类占位符

示例工具名会变成：

- `mcp-server-devops__run_shell_command`
- `mcp-server-devops__read_local_file`
- `mcp-server-ai-serving-ops__parse_serving_log`
- `mcp-server-accelerator-ops__detect_memory_pressure`
- `mcp-server-container-ops__check_container_health`

旧式点号别名仍可在手工调用里使用，例如：

```text
call mcp-server-ai-serving-ops.verify_serving_config {"config_path":"demo/vllm_config.json","framework":"vllm"}
```

## 说明

GitHub Copilot 本身更适合作为 IDE 侧的模型体验；这个脚本把“中心调度层”和“计划后执行”的运维流程搭好后，你可以替换成任意支持 tool calling 的模型提供方，也可以继续扩展专门面向 MindIE/CANN/NPU 状态检查的 MCP Server。
