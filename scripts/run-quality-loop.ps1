param(
    [string]$DocId = "",
    [int]$MaxIterations = 3,
    [int]$ReviewLimit = 12,
    [int]$ApodexPort = 8000,
    [switch]$UseApodex,
    [ValidateSet("proposal_only", "safe_auto_patch")]
    [string]$ApplyMode = "proposal_only"
)

$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $root

$argsList = @(
    "--root", ".",
    "quality-loop",
    "--max-iterations", "$MaxIterations",
    "--review-limit", "$ReviewLimit",
    "--apply-mode", $ApplyMode
)

if ($DocId.Trim().Length -gt 0) {
    $argsList += @("--doc-id", $DocId)
}

if ($UseApodex) {
    $env:APODEX_SEMANTIC_REVIEW_ENABLED = "true"
    $env:APODEX_ALLOW_EXTERNAL = "true"
    $env:APODEX_BASE_URL = "http://127.0.0.1:$ApodexPort/v1"
    $env:APODEX_API_KEY = "EMPTY"
    $env:APODEX_MODEL = "apodex/Apodex-1.0-mini"
    $env:APODEX_TIMEOUT_SECONDS = "240"
    $argsList += @("--enable-apodex", "--allow-external")
}

python -m baxter_rag.cli @argsList
