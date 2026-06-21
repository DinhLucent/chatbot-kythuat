param(
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
$url = "http://127.0.0.1:$Port/v1/models"
$connections = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
$payload = @{
    port = $Port
    listening = [bool]$connections
    process_ids = @($connections | Select-Object -ExpandProperty OwningProcess -Unique)
    base_url = "http://127.0.0.1:$Port/v1"
}
if ($connections) {
    try {
        $models = Invoke-RestMethod -Uri $url -Method Get -TimeoutSec 5
        $payload["api_ok"] = $true
        $payload["models"] = $models
    } catch {
        $payload["api_ok"] = $false
        $payload["error"] = $_.Exception.Message
    }
}
Write-Output ($payload | ConvertTo-Json -Depth 8)
