# Ascend MindIE 503 Runbook

## Scenario

MindIE or another OpenAI-compatible inference service returns HTTP 503 shortly after startup.

## Common Causes

- The model worker is still loading weights and the health endpoint becomes visible too early.
- HBM memory is almost full because another process is using the same NPU.
- The model path is wrong or the process user cannot read the weight files.
- `max_batch_size`, sequence length, or prefill settings are too aggressive for available HBM.
- The service process is alive, but backend workers failed during CANN/ACL initialization.

## Evidence to Collect

- First ERROR lines in MindIE logs before the first 503 response.
- `npu-smi info` output, especially HBM usage and residual processes.
- Model path, config path, and process user permissions.
- Service port and health endpoint status.

## Safe Actions

- Read logs and collect NPU status first.
- Confirm whether 503 disappears after model loading completes.
- Lower concurrency-related config only after backing up the original config.
- Do not kill processes or restart the service without human approval.

## Typical Diagnosis

If logs contain both `model load timeout` and HBM allocation errors, treat HBM pressure as the leading hypothesis. Check for residual benchmark or old serving processes before changing model parameters.
