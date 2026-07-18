param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$LegacyRoot = Split-Path -Parent $PSScriptRoot
$RepoRoot = Split-Path -Parent (Split-Path -Parent $LegacyRoot)

Push-Location $RepoRoot
try {
    @'
tools
diagnose {"symptom":"MindIE service returns HTTP 503 after startup and model loading times out","config_path":"legacy/mcp/mcp-server-ascend-ops/fixtures/mindie_config.json","service_name":"mindie-llm"}
exit
'@ | & $Python .\legacy\mcp\mcp-core-hub\hub.py
}
finally {
    Pop-Location
}
