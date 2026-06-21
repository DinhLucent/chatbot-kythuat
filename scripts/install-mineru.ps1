param(
    [switch]$PipelineOnly
)

$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $root

$mineruRoot = Join-Path $root "tools\MinerU"
if (-not (Test-Path $mineruRoot)) {
    git clone --depth 1 https://github.com/opendatalab/MinerU $mineruRoot
}

$venv = Join-Path $root "tools\mineru-venv"
if (-not (Test-Path $venv)) {
    python -m venv $venv
}

$python = Join-Path $venv "Scripts\python.exe"
& $python -m pip install --upgrade pip
if ($PipelineOnly) {
    & $python -m pip install -e "$mineruRoot[pipeline]"
} else {
    & $python -m pip install -e "$mineruRoot"
}

Write-Host "MinerU environment installed at $venv"
Write-Host "Use: `$env:PATH='$venv\Scripts;' + `$env:PATH"

