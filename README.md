## 项目总述

一个面向 Agent 工具调用场景的 MCP 组合式工程，提供从工具层、调度层到扩展能力层的完整示例。仓库采用多模块结构：

- 工具服务层：提供可直接被 MCP 客户端调用的工具能力
- Hub 调度层：统一接入多个 MCP Server 并把工具暴露给大模型 Agent
- 能力扩展层：提供本地文档检索（RAG）与持久化记忆（KV）等通用插件

该仓库用于快速搭建一个可扩展的 MCP Agent 基座，采取 “1个极简中央大脑 + N个独立工具插件” 的策略，致力于构建简单高效的“微型 MCP 生态框架 (Micro-MCP-Ecosystem)”。覆盖以下关键能力：

- 通过标准 MCP 协议把本地能力封装为可调用工具
- 在中心化 Hub 中聚合多个 MCP Server，形成统一工具入口
- 支持后续按需插入 RAG、长期记忆等模块，逐步演进为完整 Agent 系统

## 你能用它做什么（典型用途）

把本地能力封装成可被大模型调用的工具

- 例如：执行命令、读取本地文件（日志/配置）、查询本地知识库、读写“长期记忆”等。
- 用一个 Hub 聚合多个工具服务，形成统一入口
- 用户只需要对 Hub 输入任务，Hub 负责连接多个 MCP Server，并把它们的 tools 提供给模型选择调用。
- 按需扩展通用插件能力（RAG / 记忆 / 更多工具）
- 你可以逐步把它从“能调用工具的脚本”演进成更完整的 Agent 系统


## 模块架构说明

核心模块如下：

- `server.py`（mcp-server-devops）：命令执行与本地文件读取工具
- `mcp-core-hub/hub.py`：中心化调度层，负责连接多个 MCP Server 与 Agent 推理循环
- `mcp-server-rag-docs/server.py`：本地文档向量检索服务
- `mcp-server-memory-kv/server.py`：本地 JSON 持久化 KV 记忆服务
- `protocals/`：协议与 SDK 学习资料

调用链路（逻辑视图）：

1. 用户输入任务到 Hub。
2. Hub 基于模型决策选择工具。
3. Hub 通过 MCP 会话调用目标 Server。
4. Server 执行工具并返回结果。
5. Hub 汇总结果并返回给用户。

## 子项目说明

### 项目 1: mcp-server-devops

一个基于 Python 的 MCP Server 开发模板，当前已内置 DevOps 场景的两个工具，并带有 Human-in-the-loop 命令审批。

## 当前能力

- Tool: `run_shell_command(command: str)`
  - 通过 `subprocess.run` 执行命令
  - 执行前在服务端终端阻塞询问：`允许执行该命令吗?[y/n]`
  - 在终端打印红色告警：`[⚠️ 拦截报警] Agent 试图执行敏感命令: ...`
  - 返回 `stdout` / `stderr` / `returncode`

- Tool: `read_local_file(filepath: str)`
  - 读取本地文件内容（适合日志文件）
  - 读取失败时返回错误信息

- 传输模式
  - `stdio`（CLI/桌面客户端）
  - `sse`（HTTP + SSE）

- 内置调试页面
  - SSE 模式启动后自动打开浏览器
  - 页面地址默认是 `http://0.0.0.0:8080/`
  - 页面上可直接调用两个工具

## 运行要求

- Python 3.12+
- 依赖：
  - `mcp>=1.4.1`
  - `starlette>=0.46.1`
  - `uvicorn>=0.34.0`

## 安装

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux / macOS
# source .venv/bin/activate

pip install -e .
```

## 启动

### 1) stdio 模式

```bash
python server.py --transport stdio
```

### 2) SSE 模式（推荐调试）

```bash
python server.py --transport sse --host 0.0.0.0 --port 8080
```

可选参数：

```bash
# 关闭自动打开浏览器
python server.py --transport sse --no-open-browser
```

## 演示流程（任务 1）

1. 以 SSE 模式运行服务。
2. 浏览器会自动打开调试页面。
3. 页面中可看到两个工具：
   - `run_shell_command`
   - `read_local_file`
4. 在 `run_shell_command` 输入：`ls -la`，点击执行。
5. 查看运行 `server.py` 的终端，可见红色告警，并阻塞等待你输入 `y/n`。
6. 在终端输入 `y` 回车，页面会返回命令执行结果。

## 代码结构

- `server.py`: MCP 服务实现（DevOps tools + SSE + 调试页面）
- `pyproject.toml`: 项目元数据和依赖
- `protocals/`: MCP 协议和 SDK 文档（供开发参考）

## 安全配置

本项目不在仓库中存储任何 API Key。请通过环境变量提供密钥：

- 使用环境变量注入 API Key（见下方示例）
- 如发现密钥泄露，请立即轮换密钥

常用环境变量（供 `mcp-core-hub` 使用）：

- `MINIMAX_API_KEY`（推荐）
- `OPENAI_API_KEY`（兼容）
- `MINIMAX_BASE_URL` 或 `OPENAI_BASE_URL`（可选）
- `MCP_CORE_HUB_MODEL`（可选）

Windows PowerShell 示例：

```powershell
$env:MINIMAX_API_KEY = "your_real_api_key"
```

Windows CMD 示例：

```cmd
set MINIMAX_API_KEY=your_real_api_key
```

### 项目 2

中心化 Agent 调度层，请看 [mcp-core-hub/README.md](mcp-core-hub/README.md) 和 [mcp-core-hub/hub.py](mcp-core-hub/hub.py)。

### 项目 3

本地文档检索插件见 [mcp-server-rag-docs/README.md](mcp-server-rag-docs/README.md) 和 [mcp-server-rag-docs/server.py](mcp-server-rag-docs/server.py)。

### 项目 4

长效 KV 记忆插件见 [mcp-server-memory-kv/README.md](mcp-server-memory-kv/README.md) 和 [mcp-server-memory-kv/server.py](mcp-server-memory-kv/server.py)。
