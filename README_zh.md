# Termops — 终端运维 Agent

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](./LICENSE)

[English](./README.md)

**Termops（终端运维 Agent）** 是一个终端原生的 AI Agent，通过 Shell 钩子自动捕获命令错误，并用你选择的 LLM 分析。它以本地守护进程运行，内置审批门控动作引擎——未经你明确同意，任何操作都不会修改你的系统。

```text
  ┌──────────────────────────────────────────────────┐
  │  Shell（PowerShell / Bash / Zsh）                │
  │  ┌────────────────────────────────────────────┐  │
  │  │  $ python -c "import missing_pkg"           │  │
  │  │  ModuleNotFoundError: ...    ← 自动捕获     │  │
  │  └────────────────────────────────────────────┘  │
  │           │ 终端钩子（可选开启）                     │
  │           ▼                                      │
  │  ┌────────────────────────────────────────────┐  │
  │  │  termops 守护进程 (127.0.0.1:8923)         │  │
  │  │  ├─ LLM 归因（OpenAI / Anthropic /         │  │
  │  │  │   Ollama / OpenAI 兼容接口）              │  │
  │  │  ├─ MAPE-K 控制循环                       │  │
  │  │  ├─ 审批门控动作                            │  │
  │  │  └─ SQLite 知识库                         │  │
  │  └────────────────────────────────────────────┘  │
  │           │                                      │
  │           ▼                                      │
  │  ┌────────────────────────────────────────────┐  │
  │  │  termops CLI  /  Web UI（localhost）      │  │
  │  └────────────────────────────────────────────┘  │
  └──────────────────────────────────────────────────┘
```

---

## 功能特性

- **终端钩子** — 命令非零退出时自动捕获 stderr。用 `termops hook install` 开启，随时可用 `termops hook uninstall` 关闭。
- **多供应商 LLM** — 支持 OpenAI、Anthropic、Ollama 及任意 OpenAI 兼容接口（vLLM、LiteLLM、LocalAI 等）。你只需提供 API Key 和模型名。
- **审批门控** — 每条修复建议须经人工审核批准后才执行。动作 15 分钟过期，防重放。
- **结构化分析** — 错误指纹、证据提取、根因归因、严重度评分、可操作的下一步建议。
- **知识沉淀** — 成功案例、误报、用户反馈持久化到本地 SQLite，形成可检索的修复历史。
- **本地 Web 界面** — 仪表盘、任务时间线、审批面板、审计日志，仅监听 loopback。
- **无云依赖** — 默认使用确定性分析核心，LLM 按需开启。

---

## 快速开始

### 环境要求

- Python 3.10+
- （可选）LLM API Key，用于 AI 分析

### 安装

```bash
git clone https://github.com/user/termops.git
cd termops
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

### 启动守护进程

```bash
termops-agent --profile demo
```

### 分析错误

```bash
# 直接输入文本
termops analyze --text "ModuleNotFoundError: No module named 'click'"

# 从日志文件读取
termops analyze --file error.log

# 包装命令并捕获输出
termops run -- python -c "import missing_pkg"

# 从 stdin 管道输入
echo "ImportError: cannot import name 'foo'" | termops analyze
```

### 查看和管理任务

```bash
termops task list
termops task show <task-id>
termops task watch <task-id>
termops action approve <action-id>
termops action reject <action-id>
```

### 打开 Web 界面

```bash
termops web login
# 浏览器打开 http://127.0.0.1:8923
```

---

## 终端钩子（自动捕获）

终端钩子是一个 Shell 插件，监听命令错误并自动发送给 Termops 分析。**默认关闭**，需手动安装。

### 安装

```bash
termops hook install
```

自动检测你的 Shell（PowerShell / Bash / Zsh），提示你需要在 Shell 配置文件中添加的行。重启终端或 source 配置文件即可生效。

### 查看状态

```bash
termops hook status
```

### 卸载

```bash
termops hook uninstall
```

---

## LLM 配置

Termops 默认使用确定性分析核心。如需 AI 归因，请配置至少一个 LLM 供应商。API Key 保存在 `~/.termops/config.toml`，也可通过环境变量设置。

### 通过 CLI 配置

```bash
# OpenAI
termops config llm --provider openai --model gpt-4o --api-key sk-xxx --enable

# Anthropic
termops config llm --provider anthropic --model claude-sonnet-4-20250514 --api-key sk-ant-xxx --enable

# Ollama（本地模型，无需 API Key）
termops config llm --provider ollama --model qwen2.5:7b --enable

# 任意 OpenAI 兼容接口（vLLM、LiteLLM 等）
termops config llm --provider openai_compatible --base-url http://localhost:8080/v1 --model my-model --api-key my-key --enable
```

### 通过环境变量配置

```bash
export TERMOPS_LLM_PROVIDER=openai
export TERMOPS_LLM_API_KEY=sk-xxx
export TERMOPS_LLM_MODEL=gpt-4o
export TERMOPS_LLM_ENABLED=true
```

### 查看当前配置

```bash
termops config show
```

### 支持的后端

| 供应商 | 环境变量 | 默认模型 | 默认 URL |
|---|---|---|---|
| `openai` | `OPENAI_API_KEY` | `gpt-4o` | `https://api.openai.com/v1` |
| `anthropic` | `ANTHROPIC_API_KEY` | `claude-sonnet-4-20250514` | `https://api.anthropic.com/v1` |
| `ollama` | — | `qwen2.5:7b` | `http://localhost:11434/v1` |
| `openai_compatible` | `LLM_API_KEY` | （用户自定） | `http://localhost:8080/v1` |

---

## CLI 命令参考

```text
termops analyze [--text TEXT] [--file PATH] [--source LABEL] [--language LANG]
                [--command CMD] [--cwd DIR] [--exit-code N]
termops run [--language LANG] [--cwd DIR] -- <command...>
termops doctor
termops task list|show|watch|cancel
termops action approve|reject ACTION_ID
termops web login
termops hook install|uninstall|status
termops config llm|show
```

旧版别名 `erra` / `erra-agent` 仍作为兼容入口安装。

---

## 工作原理

### MAPE-K 控制循环

每个任务按以下状态机流转：

```text
queued → running → waiting_approval → executing → verifying
              ↘ succeeded / failed / cancelled
executing | verifying --daemon restart→ reconciling
```

### 任务状态机

```text
queued ──→ running ──→ waiting_approval ──→ executing ──→ verifying ──→ succeeded
  │            │              │                  │              │
  └──→ cancelled            ├──→ rejected       ├──→ failed     └──→ failed
                            └──→ expired
```

### 安全边界

- 动作需审批：仅允许从 `waiting_approval` 进入执行。
- 每个动作计算 SHA-256 摘要；重放、过期、篡改均被检测。
- Web UI 仅绑定 `127.0.0.1`，使用 HttpOnly + SameSite=Strict Cookie。
- 动作引擎不暴露任意 Shell、文件读取或网络调用。
- 密钥不会出现在日志、数据库或 API 响应中。

---

## 项目结构

```text
src/termops/
├── __init__.py          包入口
├── cli.py               CLI 命令（analyze、run、hook、config 等）
├── daemon.py            后台守护进程入口
├── config.py            配置加载（文件、环境变量、CLI）
├── engine.py            MAPE-K 分析引擎
├── graph.py             LangGraph 状态机
├── llm.py               LLM 供应商配置模型
├── llm_client.py        多供应商 LLM API 客户端
├── models.py            核心数据模型（Task、Action、Finding 等）
├── store.py             SQLite 状态存储与审计链
├── api.py               FastAPI REST 接口
├── web.py               Web UI 路由
├── diagnostics.py       错误指纹与分类引擎
├── providers.py         只读环境探针
├── security.py          令牌生成、哈希、脱敏
├── templates/           Jinja2 HTML 模板
└── hooks/
    ├── hook.ps1          PowerShell 终端钩子
    └── hook.sh           Bash/Zsh 终端钩子
tests/
├── conftest.py          共享 fixtures
├── test_cli.py          CLI 集成测试
├── test_engine.py       引擎与 MAPE-K 循环测试
├── test_store.py        状态存储与审计链测试
├── test_contracts.py    API 契约测试
└── test_api_web.py      Web UI 测试
```

---

## 开发

```bash
# 运行测试
pytest -q

# 代码检查与类型检查
ruff check src tests
mypy src/termops
```

---

## 许可证

MIT