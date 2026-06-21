param(
    [int]$ApiPort = 8010
)

$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$venv = Join-Path $root "tools\mineru-venv"
$mineruApi = Join-Path $venv "Scripts\mineru-api.exe"
if (-not (Test-Path $mineruApi)) {
    throw "MinerU is not installed. Run scripts\install-mineru.ps1 first."
}

$runtime = Join-Path $root ".runtime"
New-Item -ItemType Directory -Force -Path $runtime | Out-Null
$pidFile = Join-Path $runtime "mineru-api.pid"
$logFile = Join-Path $runtime "mineru-api.log"

$process = Start-Process -FilePath $mineruApi -ArgumentList @("--host", "127.0.0.1", "--port", "$ApiPort") -PassThru -WindowStyle Hidden -RedirectStandardOutput $logFile -RedirectStandardError $logFile
$process.Id | Set-Content -Encoding ASCII $pidFile
"`$env:MINERU_API_URL='http://127.0.0.1:$ApiPort'" | Set-Content -Encoding ASCII (Join-Path $runtime "mineru.env.ps1")
Write-Host "MinerU API started on http://127.0.0.1:$ApiPort"

