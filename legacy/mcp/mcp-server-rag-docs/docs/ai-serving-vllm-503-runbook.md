# AI Serving vLLM 503 Runbook

## Scenario

An OpenAI-compatible vLLM service starts in a container, but `/v1/models` or completion endpoints return HTTP 503.

## Common Causes

- The HTTP server is up, but model workers are still loading weights.
- CUDA out of memory occurs during model load.
- GPU memory is occupied by stale serving or benchmark processes.
- `gpu_memory_utilization` is too aggressive and leaves little runtime headroom.
- `max_model_len` is too high, increasing KV cache memory.
- The model directory is missing or not mounted into the container.
- Container port mapping exposes the wrong port.

## Evidence to Collect

- Service health response and HTTP status.
- Container logs around the first 503.
- Docker inspect output for health status, ports, mounts, and env vars.
- `nvidia-smi` or `npu-smi info` output, especially memory usage and residual processes.
- Serving config keys such as `model_path`, `gpu_memory_utilization`, `max_model_len`, and `tensor_parallel_size`.

## Safe Actions

- Read logs, inspect container state, and collect accelerator status first.
- Validate the model path and container mount before changing config.
- Preview config changes with dry-run before writing.
- Lower `gpu_memory_utilization` or `max_model_len` only after backing up the config.
- Restart the container only after approval and after a rollback path exists.

## Typical Diagnosis

If logs show both HTTP 503 and CUDA out of memory during model loading, treat accelerator memory pressure as the leading hypothesis. Check for stale GPU processes before reducing context length or memory utilization.
