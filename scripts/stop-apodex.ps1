param(
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
$connections = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if (-not $connections) {
    Write-Output (@{ ok = $true; stopped = 0; reason = "not_running"; port = $Port } | ConvertTo-Json -Compress)
    exit 0
}

$processIds = @($connections | Select-Object -ExpandProperty OwningProcess -Unique)
foreach ($processId in $processIds) {
    if ($processId -gt 0) {
        Stop-Process -Id $processId -Force
    }
}

Write-Output (@{ ok = $true; stopped = $processIds.Count; port = $Port; process_ids = $processIds } | ConvertTo-Json -Compress)
