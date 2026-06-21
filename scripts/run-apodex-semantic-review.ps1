param(
    [string]$DocId = "",
    [int]$Limit = 2,
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $root

$env:APODEX_SEMANTIC_REVIEW_ENABLED = "true"
$env:APODEX_ALLOW_EXTERNAL = "true"
$env:APODEX_BASE_URL = "http://127.0.0.1:$Port/v1"
$env:APODEX_API_KEY = "EMPTY"
$env:APODEX_MODEL = "apodex/Apodex-1.0-mini"
$env:APODEX_TIMEOUT_SECONDS = "240"

$argsList = @("--root", ".", "semantic-review", "--limit", "$Limit", "--enable-apodex", "--allow-external")
if ($DocId.Trim().Length -gt 0) {
    $argsList += @("--doc-id", $DocId)
}

python -m baxter_rag.cli @argsList
