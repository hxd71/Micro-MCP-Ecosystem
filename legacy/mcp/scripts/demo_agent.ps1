param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$LegacyRoot = Split-Path -Parent $PSScriptRoot
$RepoRoot = Split-Path -Parent (Split-Path -Parent $LegacyRoot)

if (-not $env:MINIMAX_API_KEY -and -not $env:OPENAI_API_KEY) {
    Write-Host "MINIMAX_API_KEY or OPENAI_API_KEY is required for LLM Ops Agent mode."
    Write-Host 'Example: $env:MINIMAX_API_KEY = "your_key"'
    exit 1
}

Push-Location $RepoRoot
try {
    & $Python .\legacy\mcp\mcp-core-hub\hub.py
}
finally {
    Pop-Location
}
