param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$LegacyRoot = Split-Path -Parent $PSScriptRoot
$RepoRoot = Split-Path -Parent (Split-Path -Parent $LegacyRoot)

Push-Location $RepoRoot
$oldForceMock = $env:ACCELERATOR_OPS_FORCE_MOCK
try {
    $env:ACCELERATOR_OPS_FORCE_MOCK = "true"
    @'
tools
diagnose {"symptom":"vLLM container vllm-qwen returns HTTP 503 from /v1/models after startup; diagnose and preview safe repair","service_url":"http://127.0.0.1:8000/v1/models","log_path":"demo/vllm_503_gpu_memory.log","config_path":"demo/vllm_config.json","model_path":"demo/models/Qwen2.5-7B-Instruct","container":"vllm-qwen","accelerator_provider":"auto","framework":"vllm"}
exit
'@ | & $Python .\legacy\mcp\mcp-core-hub\hub.py
}
finally {
    if ($null -eq $oldForceMock) {
        Remove-Item Env:ACCELERATOR_OPS_FORCE_MOCK -ErrorAction SilentlyContinue
    }
    else {
        $env:ACCELERATOR_OPS_FORCE_MOCK = $oldForceMock
    }
    Pop-Location
}
