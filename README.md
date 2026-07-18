# Local AI Ops Agent

面向 Linux 单节点、Docker、NVIDIA GPU 和 vLLM 的本地自治运维 Agent。它以确定性工作流完成部署预检、运行诊断、GPU/容器证据收集、安全检查和恢复验证；任何会改变节点状态的操作都会停在不可变审批提案上。

项目不再把“多个 MCP Server 能互相调用”当作核心成果。生产主路径是一个可持久化、可恢复、可审计的本地 control plane：

```text
CLI / Local Web UI
        |
        v
FastAPI daemon over Unix socket + 127.0.0.1
        |
        +-- Task state machine / SQLite WAL / audit hash chain
        +-- Policy engine / approval digest / expiry / replay protection
        +-- Docker Engine provider
        +-- NVIDIA NVML provider
        +-- vLLM diagnosis and deployment workflow

Deprecated read-only MCP shim (`server.py`) ---> capability/manifest validation only
```

MCP 不是 daemon 内部总线，也不是生产部署单元。这样可避免多进程 Server 编排、松散 JSON
契约和工具权限扩散进入变更路径。历史 Hub、MCP Server、Ascend/RAG/Memory KV、协议资料和
离线 demo 已整体归档到 `legacy/mcp/`，不会被主 wheel 打包或被 `live` profile 加载。

## 核心能力

- **声明式 vLLM 部署**：读取 `InferenceService` YAML，执行 Docker、GPU、模型路径、端口、镜像和安全策略预检。
- **审批后执行**：部署、镜像拉取、重启、参数调整和回滚都必须审批；审批绑定 action digest，15 分钟过期且只能使用一次。
- **验证失败回滚**：更新时保留旧容器。新版本未通过 `/v1/models` 等健康检查时，执行审批提案中预先声明的回滚。
- **证据化诊断**：HTTP、Docker state/logs、NVML 和受管 vLLM 配置分别形成 Observation；Finding 必须引用 Observation ID。
- **GPU 与容器安全基线**：识别 GPU OOM/高显存、特权容器、host namespace、敏感挂载、额外 capabilities、公开端口和未固定镜像。
- **持久化任务**：SQLite WAL 保存 task、observation、finding、action、revision 和 audit event；daemon 重启后 reconciliation 中断任务。
- **最小主动巡检**：只巡检受管 vLLM 服务、容器和 GPU；连续失败后创建只读 incident，不自动修复。
- **本地 Web UI**：展示能力、服务、GPU、任务、证据、Finding、审批和审计。UI 只能监听 loopback。
- **无云依赖**：生产默认不调用 LLM，不需要 API Key，也不会把节点证据发送到外部服务。

## 安全边界

生产核心不存在任意 shell、任意文件读取、任意 URL 或 LLM tool calling。动作只来自有限的强类型注册表：部署受管 revision、重启受管容器、调整受支持的 vLLM 参数和恢复已知 revision。

- `live`、`test`、`demo` 是显式 profile。
- `live` 无法连接 Docker/NVML 时返回 unavailable，不会加载 fixture。
- `live` 要求镜像使用 `@sha256:` 固定 digest。
- 模型和 secret 文件必须位于配置的 allowlist 根目录。
- 健康探测只允许 loopback 或配置的 CIDR，避免 SSRF。
- secret 值不会出现在 API、数据库、日志或审计事件中。
- Web 登录使用 `aiops web login` 生成的单次链接，会话 cookie 为 HttpOnly + SameSite=Strict，变更请求要求 CSRF token。
- Trivy 是可选能力；未安装时安全扫描返回 `SCANNER_UNAVAILABLE`，不会把“未扫描”视为通过。

Docker socket 本身等价于宿主机高权限。生产 systemd 单元将 daemon 放在独立 `aiops` 用户下，并限制文件系统写入范围，但加入 `docker` 组仍应视为受信任的本地运维权限。

## 快速体验

要求 Python 3.10+（可直接使用 Ubuntu 22.04 的系统 Python 创建 venv）。

```powershell
python -m venv .venv
.venv\Scripts\activate
python -m pip install -e ".[dev,mcp]"
```

启动显式 demo profile。它不会连接真实 Docker 或 GPU，也不会被 `live` 自动使用：

```powershell
aiops-agent --profile demo --no-uds
```

另一个终端执行：

```powershell
aiops --profile demo doctor
aiops --profile demo deploy examples/vllm-service.demo.yaml
aiops --profile demo task list
```

部署任务完成预检后会进入 `waiting_approval`。查看任务可以得到 action ID：

```powershell
aiops --profile demo task show <task-id>
aiops --profile demo action approve <action-id>
aiops --profile demo task watch <task-id>
```

生成一次性 Web 登录链接：

```powershell
aiops --profile demo web login
```

Web UI 默认地址为 `http://127.0.0.1:8787`。页面支持发起诊断、查看实时任务事件以及批准或拒绝已有变更提案；部署 YAML 仍只能通过 CLI 提交。

## Linux 生产部署

1. 安装 Docker Engine、NVIDIA 驱动和 NVIDIA Container Toolkit，并确认目标 vLLM 镜像已经按 digest 固定。
2. 把模型放到策略允许的目录，例如 `/models/Qwen2.5-7B-Instruct`。
3. 安装项目并复制 [配置示例](config/aiops-agent.toml.example) 到 `/etc/aiops-agent/aiops-agent.toml`。
4. 根据实际路径修改 [systemd unit](deploy/systemd/aiops-agent.service)，创建 `aiops` 和 `aiops-operators` 组后启动服务。
5. 根据 [服务清单示例](examples/vllm-service.yaml) 创建真实 manifest，替换镜像 digest 和模型路径。

```bash
sudo systemctl enable --now aiops-agent
aiops doctor
aiops deploy /etc/aiops-agent/services/qwen.yaml
aiops task watch <task-id>
```

daemon 默认同时提供：

- `/run/aiops-agent/agent.sock`：CLI 使用的完整 HTTP/JSON API。
- `127.0.0.1:8787`：带 token/session 保护的 API 和 Web UI。

v1 会拒绝把 Web UI 绑定到非 loopback 地址。

## InferenceService

最小 manifest：

```yaml
apiVersion: aiops.local/v1alpha1
kind: InferenceService
metadata:
  name: qwen-vllm
spec:
  image: vllm/vllm-openai@sha256:<verified-digest>
  model:
    hostPath: /models/Qwen2.5-7B-Instruct
    containerPath: /model
  gpu:
    deviceIds: ["0"]
  endpoint:
    bindAddress: 127.0.0.1
    hostPort: 8000
    healthPath: /v1/models
  vllm:
    launchMode: image-entrypoint
    engineVersion: auto
    tensorParallelSize: 1
    maxModelLen: 8192
    gpuMemoryUtilization: 0.90
    enforceEager: false
    maxNumSeqs: 32
    maxNumBatchedTokens: 8192
    swapSpaceGiB: 4
  monitoring:
    intervalSeconds: 60
    failureThreshold: 3
    startupTimeoutSeconds: 300
```

不支持通过 manifest 传递任意 shell 或任意 vLLM 参数。secret 仅接受策略目录内的文件引用，并以只读文件挂载到 `/run/secrets/<NAME>`。

`launchMode` 只能选择镜像入口或固定的 `python -m vllm.entrypoints.openai.api_server`；
`engineVersion` 只能选择 `auto/v0/v1`。这些字段用于兼容经过验证的 vLLM 镜像，不允许自定义
entrypoint、环境变量或 argv。低显存机器不能把 `gpuMemoryUtilization` 简单地越调越低：Agent
会结合 HTTP、OOM、日志和 GPU 容量，再决定预留比例与 context/concurrency 上限。

## CLI

```text
aiops doctor
aiops deploy MANIFEST
aiops diagnose SERVICE [--symptom TEXT]
aiops security scan SERVICE
aiops task list|show|watch
aiops action approve|reject ACTION_ID
aiops rollback REVISION_ID
aiops web login
```

CLI 与 Web UI 使用相同 task/action 数据，不存在绕过审批的第二套执行路径。

## 状态机

```text
queued -> running -> waiting_approval -> executing -> verifying
                \-> succeeded / failed / rolled_back / cancelled
executing|verifying --daemon restart--> reconciling
```

审批只允许从 `waiting_approval` 进入执行。拒绝、过期、digest 不一致和重复提交都不会调用 Docker mutation API。

## 测试

```powershell
pytest -q
ruff check src tests
mypy src/aiops_agent
```

自动化测试覆盖 manifest/schema 契约、路径/探测策略、脱敏、审计链、审批摘要与重放保护、
部署成功、Docker 不可用、daemon 重启 reconciliation、验证失败回滚、任务取消、Finding 证据引用、
NVIDIA CLI 只读回退、Trivy 不可用、API token、Web 登录、SSE 事件和 CSRF。Linux CI 在
Python 3.10 与 3.12 上运行 Ruff、Mypy、Pytest、API/schema 契约检查以及 wheel/sdist 构建。

CI 不伪造 GPU 生产验收。真实 Linux NVIDIA 节点还应执行：

- `aiops doctor` 能识别 Docker Engine、NVIDIA driver 和目标 GPU。
- 使用本地模型和固定 digest 镜像完成 vLLM 部署。
- `/v1/models` 返回 200 后 revision 才能标记 active。
- 提交一个健康检查必然失败的新 revision，确认旧容器自动恢复。

本机 RTX 3050 + WSL2 已完成真实 CUDA 模型加载、`/v1/models` 和 completion 请求，结果与
4GB 显存参数记录见 [硬件验收报告](docs/hardware-acceptance-rtx3050-wsl2.md) 和
[对应 manifest](examples/vllm-service.rtx3050-wsl2.yaml)。独立的 GitHub-hosted Ubuntu 22.04
systemd 作业负责验证生产 unit 的安装、操作员权限、启停和崩溃恢复，范围与证据说明见
[原生 Linux systemd 验收](docs/native-linux-systemd-acceptance.md)。

## 项目结构

```text
src/aiops_agent/       daemon、CLI、模型、存储、provider、执行引擎和 Web UI
config/                 生产配置示例
deploy/systemd/         Linux systemd unit
examples/               live/demo InferenceService 清单
tests/                  单元与集成测试
docs/                   运行与真实硬件验收记录
legacy/mcp/             一版兼容期内的旧 Hub、MCP Server、Ascend/RAG/Memory 和 demo
```

根目录 `server.py` 现在只是只读 MCP 兼容入口，仅保留能力检查和 manifest 校验。旧 Hub、Memory KV、FAISS RAG、Ascend/MindIE/NPU 以及原调试页面均不属于 v1 生产主路径。
