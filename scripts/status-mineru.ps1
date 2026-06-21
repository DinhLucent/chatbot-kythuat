$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$pidFile = Join-Path $root ".runtime\mineru-api.pid"
if (-not (Test-Path $pidFile)) {
    Write-Host "MinerU API pid file not found."
    exit 1
}
$pidValue = Get-Content $pidFile -ErrorAction SilentlyContinue
$proc = Get-Process -Id $pidValue -ErrorAction SilentlyContinue
if ($proc) {
    Write-Host "MinerU API running. PID=$pidValue"
    exit 0
}
Write-Host "MinerU API is not running."
exit 1

