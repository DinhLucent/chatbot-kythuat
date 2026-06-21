param(
    [int]$Port = 8000,
    [int]$CtxSize = 4096,
    [int]$Threads = 8,
    [string]$ModelPath = "",
    [string]$Alias = "apodex/Apodex-1.0-mini"
)

$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$server = Join-Path $root "tools\llama.cpp\b9743-cpu-x64\llama-server.exe"
if ($ModelPath.Trim().Length -eq 0) {
    $ModelPath = Join-Path $root "tools\models\apodex-1.0-mini-q4_k_m\apodex-1.0-mini-q4_k_m.gguf"
}
$logDir = Join-Path $root "logs"
$outLog = Join-Path $logDir "apodex-$Port.out.log"
$errLog = Join-Path $logDir "apodex-$Port.err.log"

if (-not (Test-Path -LiteralPath $server)) {
    throw "llama-server.exe not found: $server"
}
if (-not (Test-Path -LiteralPath $ModelPath)) {
    throw "Apodex GGUF model not found: $ModelPath"
}
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$existing = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($existing) {
    $owners = ($existing | Select-Object -ExpandProperty OwningProcess -Unique) -join ", "
    throw "Port $Port is already in use by process id(s): $owners"
}

$argsList = @(
    "--model", $ModelPath,
    "--alias", $Alias,
    "--host", "127.0.0.1",
    "--port", "$Port",
    "--ctx-size", "$CtxSize",
    "--threads", "$Threads",
    "--batch-size", "512",
    "--jinja",
    "--reasoning", "off",
    "--log-file", $outLog
)

$process = Start-Process -FilePath $server `
    -ArgumentList $argsList `
    -WorkingDirectory (Split-Path -Parent $server) `
    -RedirectStandardOutput $outLog `
    -RedirectStandardError $errLog `
    -WindowStyle Hidden `
    -PassThru

Write-Output (@{
    ok = $true
    pid = $process.Id
    port = $Port
    base_url = "http://127.0.0.1:$Port/v1"
    model = $Alias
    model_path = $ModelPath
    out_log = $outLog
    err_log = $errLog
} | ConvertTo-Json -Compress)
