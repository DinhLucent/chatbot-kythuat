$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$pidFile = Join-Path $root ".runtime\mineru-api.pid"
if (Test-Path $pidFile) {
    $pidValue = Get-Content $pidFile -ErrorAction SilentlyContinue
    $proc = Get-Process -Id $pidValue -ErrorAction SilentlyContinue
    if ($proc) {
        Stop-Process -Id $pidValue -Force
    }
    Remove-Item -LiteralPath $pidFile -Force
}
Write-Host "MinerU API stopped."

