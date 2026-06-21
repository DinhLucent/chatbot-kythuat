param(
    [int]$Limit = 0
)

$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $root

$limitArgs = @()
if ($Limit -gt 0) {
    $limitArgs = @("--limit", "$Limit")
}

python -m baxter_rag.cli --root . inventory
python -m baxter_rag.cli --root . convert @limitArgs
python -m baxter_rag.cli --root . normalize @limitArgs
python -m baxter_rag.cli --root . rebuild-index
python -m baxter_rag.cli --root . eval-seed --count 96

