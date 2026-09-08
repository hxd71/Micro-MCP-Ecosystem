# Termops — 能看懂终端的 AI Agent

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](./LICENSE)

[English](./README.md)

## 为什么做这个

常在终端里干活的人都有过这种时刻：命令失败了，屏幕刷出一段红字，于是复制错误、粘到搜索引擎或聊天窗口，等答案，执行修复，再失败，再复制。这个循环完全靠手工，而且每一次切换上下文，都在打断你真正在做的事。

Termops 把这个循环自动化。它是一个跑在本机的守护进程，接收你终端里的错误输出——可以由 Shell 钩子自动捕获，也可以手动粘贴——然后像一个有经验的同事那样处理它：认出错误、结合环境、给出根因和修复建议。而和聊天机器人不同的是，**它不会擅自动手**：任何可能改变你系统的命令，都必须先经过你的批准才会执行。

一句话概括：Termops = 自动捕获错误 + 确定性规则分类 + LLM 根因归因 + 人工审批门控。

## 它是如何分析一段终端输出的

一段错误进来后，Termops 不是把原文整个丢给模型自由发挥，而是走一条清晰的流水线（内部是 MAPE-K 控制循环，用 LangGraph 实现）：

```text
捕获 ──▶ 分类 ──▶ 归因 ──▶ 计划 ──▶ 审批 ──▶ 执行与验证 ──▶ 知识沉淀
```

1. **捕获** — 收集的不只是 stderr：还有执行的命令、退出码、工作目录，以及一份环境快照（操作系统、解释器、相关环境变量）。上下文决定分析质量。
2. **分类** — 内置的确定性模式库先做一遍指纹匹配：`ModuleNotFoundError`、`permission denied`、`EADDRINUSE`、`non-fast-forward`……每种模式对应严重度和默认建议。这一层不依赖网络、不依赖模型，所以即使不配置任何 LLM，Termops 也能给出可读的分析（下文所有截图均为该模式）。
3. **归因** — 这里才轮到 LLM。模型同时拿到错误原文、规则层的 findings 和环境快照，返回一段结构化 JSON：最可能的根因、置信度、修复步骤，以及（如有必要）一条建议的验证命令。模型未配置或调用失败时，自动降级为规则层结果，不阻塞主流程。
4. **计划与审批** — 任何会改变状态的动作都被冻结成一条带 SHA-256 摘要的「待审批」动作，等你 `approve`；15 分钟过期，不可重放。
5. **知识沉淀** — 哪次修复成功、哪条 finding 是误报、你 approve/reject 的决定，都会写入本地 SQLite。下一次相似错误进来时，归因层有「记忆」可查。

## 快速开始

### 安装

```bash
git clone https://github.com/hxd71/Termops.git
cd Termops
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

只需要 Python 3.10+。装完即可使用——规则核心不依赖任何外部服务。

### 启动守护进程，跑第一次分析

```bash
termops-agent --profile demo

# 另一个终端里
termops analyze --text "ModuleNotFoundError: No module named 'click'"
```

除了 `--text`，还可以这样喂错误：

```bash
termops analyze --file error.log           # 从日志文件读
termops run -- python -c "import missing"  # 包装命令，自动捕获它的输出
echo "ImportError: ..." | termops analyze  # 从 stdin 管道
```

### 任务与审批

每次分析都是一个任务，生命周期为：

```text
queued → running → waiting_approval → executing → verifying → succeeded
```

```bash
termops task list                    # 列出任务
termops task show <task-id>          # 查看 findings 与待审批动作
termops task watch <task-id>         # 实时跟踪状态流转
termops action approve <action-id>   # 批准动作
termops action reject  <action-id>   # 拒绝
```

### Web 界面

```bash
termops web login   # 生成一次性登录链接
```

浏览器打开 `http://127.0.0.1:8923`（仅 loopback），有仪表盘、任务时间线和审批面板。

## LLM 配置：Agent 的思考核心

上面这套流程用确定性规则核心就能跑通，适合先上手体验；但 **Termops 真正的价值在 LLM 归因层——你接的模型就是这个 Agent 的大脑**。部署时最重要的一步，就是告诉它用哪个模型。

### 一行命令配置

```bash
# OpenAI
termops config llm --provider openai --model gpt-4o --api-key sk-xxx --enable

# Anthropic
termops config llm --provider anthropic --model claude-sonnet-4-20250514 --api-key sk-ant-xxx --enable

# Ollama 本地模型，无需 API Key，完全离线
termops config llm --provider ollama --model qwen2.5:7b --enable

# 任意 OpenAI 兼容接口（vLLM、LiteLLM、LocalAI……）
termops config llm --provider openai_compatible --base-url http://localhost:8080/v1 --model my-model --api-key my-key --enable
```

配置写入 `~/.termops/config.toml`。也可以用环境变量，更适合容器 / CI：

```bash
export TERMOPS_LLM_PROVIDER=openai
export TERMOPS_LLM_API_KEY=sk-xxx
export TERMOPS_LLM_MODEL=gpt-4o
export TERMOPS_LLM_ENABLED=true
```

`termops config show` 查看当前生效配置；`termops doctor` 会直接告诉你 LLM 是否已接通。

### 支持的供应商

| 供应商 | Key 环境变量 | 默认模型 | 默认地址 |
|---|---|---|---|
| `openai` | `OPENAI_API_KEY` | `gpt-4o` | `https://api.openai.com/v1` |
| `anthropic` | `ANTHROPIC_API_KEY` | `claude-sonnet-4-20250514` | `https://api.anthropic.com/v1` |
| `ollama` | — | `qwen2.5:7b` | `http://localhost:11434/v1` |
| `openai_compatible` | `LLM_API_KEY` | （自定） | `http://localhost:8080/v1` |

Key 以明文存于 `~/.termops/config.toml`（写入时自动 `chmod 0600`）或环境变量 `TERMOPS_LLM_API_KEY`，二选一：不写日志、不存数据库、不出现在任何 API 响应中。

---

## 真实运行截图

以下均为本机真实输出（`--profile demo`，规则核心，未启用 LLM），展示的是「不接模型也能用」的基线效果。

### 环境检查

```bash
termops --profile demo doctor
```

![termops doctor](screenshots/termops-doctor.png)

### 错误分析

Python 缺依赖：

```bash
termops analyze --text "Traceback ... ModuleNotFoundError: No module named 'flask'" --command "python app.py" --exit-code 1
```

![termops analyze python](screenshots/termops-analyze-python.png)

git push 被拒绝：

![termops analyze git](screenshots/termops-analyze-git.png)

Docker 守护进程不可达：

![termops analyze docker](screenshots/termops-analyze-docker.png)

端口被占用（EADDRINUSE）：

![termops analyze port](screenshots/termops-analyze-port.png)

### 任务管理

```bash
termops task list
termops task show <task-id>
```

![termops task list](screenshots/termops-task-list.png)

![termops task show](screenshots/termops-task-show.png)

### Web 界面

`termops web login` 生成一次性登录链接，浏览器访问 `http://127.0.0.1:8923`（仅 loopback）。

仪表盘（运行总览）：

![web dashboard](screenshots/termops-web-dashboard.png)

任务列表：

![web tasks](screenshots/termops-web-tasks.png)

任务详情（MAPE-K 时间线与证据）：

![web task detail](screenshots/termops-web-task.png)

---

## 终端钩子：让终端自己上报错误

钩子是一段 Shell 插件：命令非零退出时，自动把 stderr 发给 Termops。**默认关闭**，不主动安装就不会捕获任何东西。

```bash
termops hook install    # 自动识别 PowerShell / Bash / Zsh，提示要加进配置文件的行
termops hook status
termops hook uninstall  # 随时移除
```

重启终端或 source 配置文件后生效。

## 安全边界

- 审批门控：动作只有 `approve` 后才执行；15 分钟过期，SHA-256 摘要防重放、防篡改。
- 动作引擎不暴露任意 Shell、文件读取或网络调用能力。
- Web UI 仅绑定 `127.0.0.1`，HttpOnly + SameSite=Strict Cookie。
- 密钥不写日志、不存数据库、不出现在 API 响应中。

## CLI 速查

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

## 项目结构

```text
src/termops/
├── cli.py               CLI 命令（analyze、run、hook、config 等）
├── daemon.py            后台守护进程入口
├── config.py            配置加载（文件、环境变量、CLI）
├── engine.py            MAPE-K 分析引擎
├── graph.py             LangGraph 状态机
├── diagnostics.py       错误指纹与确定性分类
├── llm.py               LLM 供应商配置模型
├── llm_client.py        多供应商 LLM 客户端（失败自动降级）
├── models.py            核心数据模型（Task、Action、Finding 等）
├── store.py             SQLite 状态存储与审计链
├── api.py               FastAPI REST 接口
├── web.py               Web UI 路由
├── providers.py         只读环境探针
├── security.py          令牌生成、哈希、脱敏
├── templates/           Jinja2 HTML 模板
└── hooks/
    ├── hook.ps1          PowerShell 终端钩子
    └── hook.sh           Bash/Zsh 终端钩子
tests/
├── test_cli.py          CLI 集成测试
├── test_engine.py       引擎与 MAPE-K 循环测试
├── test_store.py        状态存储与审计链测试
├── test_contracts.py    API 契约测试
└── test_api_web.py      Web UI 测试
```

## 开发

```bash
pytest -q                # 测试
ruff check src tests     # lint
mypy src/termops         # 类型检查
```

## 许可证

MIT