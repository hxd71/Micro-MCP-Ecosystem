# mcp-core-hub

一个极简的中心化 MCP Agent 调用脚本，职责是：

- 读取 `config.json`
- 启动并连接多个 MCP Server
- 把这些 Server 暴露出来的 Tools 交给 LangGraph
- 进入 `while True` 交互循环，持续接收用户输入
- 让具备原生 tool calling 能力的模型自动决定是否调用工具

## 当前定位

这个 hub 是项目 2 的中心层，默认先接入项目 1：

- `mcp-server-devops`：本地命令与文件读取工具
- `mcp-server-rag-docs`：后续用于本地文档检索
- `mcp-server-memory-kv`：后续用于长效状态存储

目前仓库里已经有的是 `mcp-server-devops`，后两个可以先保持 `enabled: false`，等你后面补齐后再打开。

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
	"servers": [
		{
			"name": "mcp-server-devops",
			"enabled": true,
			"script": "../server.py",
			"args": ["--transport", "stdio"],
			"env": {
				"MCP_APPROVAL_MODE": "auto"
			}
		}
	]
}
```

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

- `mcp-server-devops.run_shell_command`
- `mcp-server-devops.read_local_file`

## 说明

GitHub Copilot 本身更适合作为 IDE 侧的模型体验；这个脚本把“中心调度层”搭好后，你可以替换成任意支持 tool calling 的模型提供方。