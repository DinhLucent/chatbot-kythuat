param(
  [int]$Port = 8090
)
$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $root
$env:APP_PORT = "$Port"
python -m baxter_rag.cli --root . serve
