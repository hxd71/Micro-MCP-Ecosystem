# RTX 3050 WSL2 hardware acceptance

Date: 2026-07-17

This is a real local hardware run, not a fixture or mocked GPU result.

## Environment

- Windows host with Ubuntu 22.04 on WSL2
- WSL kernel `6.6.87.2-microsoft-standard-WSL2`
- NVIDIA GeForce RTX 3050 Laptop GPU, 4096 MiB
- NVIDIA KMD `610.74`, CUDA UMD `13.3`
- Docker Engine `28.5.1` inside WSL
- vLLM `0.8.3` in the pinned ModelScope image used by
  `examples/vllm-service.rtx3050-wsl2.yaml`
- Local `Qwen2.5-0.5B-Instruct` safetensors model

## Results

1. An ephemeral `--gpus all` container detected the physical NVIDIA GPU.
2. Direct `vllm.LLM` initialization loaded 0.9277 GiB of model weights and generated text on CUDA.
3. The OpenAI-compatible HTTP server returned HTTP 200 from `/v1/models`.
4. `/v1/completions` generated 24 tokens successfully from a 4-token prompt.
5. `nvidia-smi` observed 3813 MiB / 4096 MiB while the model server was active.
6. The server reserved approximately 2.61 GiB for KV cache at `gpuMemoryUtilization=0.90`.
7. A live-profile Agent deployment stopped at `waiting_approval`; approving its immutable action digest
   created a managed revision and completed with `succeeded` only after `/v1/models` returned 200.
8. A diagnosis while the healthy server held high GPU memory completed with no Finding and no action,
   confirming that expected KV-cache reservation is not treated as an incident by itself.
9. An approved candidate using the deliberately invalid `/not-ready` health path failed verification and
   completed as `rolled_back`, restoring the prior healthy revision.
10. After acceptance, all temporary Agent/vLLM containers and volumes were removed. The final check showed
    no running Docker containers, no GPU compute processes and 0 MiB / 4096 MiB GPU memory in use.

Validated low-VRAM settings:

```text
dtype=half
maxModelLen=512
gpuMemoryUtilization=0.90
enforceEager=true
maxNumSeqs=4
maxNumBatchedTokens=512
swapSpaceGiB=1
engineVersion=v0
```

## Findings

- `gpuMemoryUtilization=0.70` failed with `No available memory for the cache blocks`; lowering the
  reservation ratio is not a universally safe OOM response.
- High GPU memory usage is expected when a healthy vLLM process reserves KV cache. Diagnosis must
  correlate memory metrics with health, Docker OOM state and log evidence.
- Reading a roughly 1 GiB model from `/mnt/f` made cold start take about 98 seconds. A production WSL
  deployment should store models on the Linux ext4 filesystem under an allowed model root.
- This WSL installation reports systemd startup delays when no foreground WSL client remains. Native
  Linux systemd acceptance is still a distinct deployment check; the GPU, Docker and HTTP inference
  path itself passed on this node.

The test manifest is intentionally hardware-specific. Do not copy its context, concurrency or engine
version settings to a larger model or GPU without a fresh capacity check.
