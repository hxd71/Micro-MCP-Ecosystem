# mcp-server-ascend-ops

面向昇腾推理运维场景的 MCP Server。它把常见的只读诊断动作封装成工具，供 `mcp-core-hub` 在计划确认后调用。

## 解决的问题

当 MindIE/CANN 推理服务出现 503、模型加载超时、NPU HBM 占用过高、CANN 环境变量缺失等问题时，排障通常要切换多个入口：日志、`npu-smi`、环境变量、接口健康检查、历史案例。这个插件把这些入口收敛成一组 MCP 工具，并输出结构化证据和下一步动作建议。

## Tools

- `check_cann_env()`
  - 检查 `ASCEND_HOME_PATH`、`ASCEND_OPP_PATH`、`PATH`、`PYTHONPATH` 等环境变量。
- `check_npu_status_info(use_mock_when_unavailable: bool = True)`
  - 优先执行 `npu-smi info`，没有真实硬件时回退到内置 mock 输出，便于演示。
- `parse_mindie_log(log_path: str = "")`
  - 解析 MindIE/CANN 风格日志，提取 503、模型加载超时、HBM 内存异常、CANN 初始化异常、端口冲突等证据。
- `inspect_inference_health(url: str, timeout_seconds: float = 3.0)`
  - 检查推理服务 HTTP 健康接口或 OpenAI-compatible endpoint。
- `diagnose_ascend_inference_issue(symptom: str, log_path: str = "")`
  - 综合用户现象、日志、环境和 NPU 状态，输出根因假设、置信度和下一步动作。
- `generate_ascend_remediation_plan(symptom: str, log_path: str = "")`
  - 生成面向修复的计划，区分只读检查、低风险动作和需要人工审批的动作。
- `verify_mindie_config(config_path: str = "")`
  - 校验 MindIE 风格 JSON 配置，提示高风险参数。
- `suggest_mindie_config_patch(config_path: str = "", symptom: str = "")`
  - 针对 HBM 压力、模型加载超时等现象生成受限配置补丁建议。
- `backup_mindie_config(config_path: str = "")`
  - 修改前创建带时间戳的配置备份。
- `apply_mindie_config_patch(config_path: str = "", patch_json: str = "", dry_run: bool = True)`
  - 预览或应用受限配置补丁。默认 `dry_run=true`，不会写文件。
- `restart_service_plan(service_name: str, restart_command: str = "")`
  - 生成重启服务的高风险执行计划，不直接执行。

## Standalone Run

```bash
python server.py --transport stdio
```

## Demo Log

内置样例日志：

```text
fixtures/mindie_503_model_load_timeout.log
```

它模拟了一个 MindIE 服务返回 503、模型加载超时且 NPU HBM 内存分配失败的场景。没有昇腾硬件的本地环境也能基于这份日志演示完整诊断链路。

## Safety Boundary

默认只允许读取 `fixtures/` 和 `ASCEND_OPS_WORKSPACE` 下的配置文件。真正写配置时，目标文件必须位于 `ASCEND_OPS_WORKSPACE` 内；内置 fixture 默认只用于演示和 dry-run。

```powershell
$env:ASCEND_OPS_WORKSPACE = "F:\your-safe-config-workspace"
```

`apply_mindie_config_patch` 只允许修改少量白名单字段，例如 `max_batch_size`、`max_prefill_tokens`、`model_load_timeout_ms` 和 `health_check_timeout_ms`。服务重启不会在本插件里直接执行，需要通过 Hub 审批后再交给 DevOps 命令工具。
