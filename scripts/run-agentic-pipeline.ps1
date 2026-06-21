param(
    [int]$Limit = 0,
    [string]$DocId = "",
    [switch]$EnablePaddleOCR,
    [switch]$EnableHyperExtract,
    [switch]$EnableApodex
)

$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $root

$argsList = @("--root", ".", "pipeline-run")
if ($Limit -gt 0) {
    $argsList += @("--limit", "$Limit")
}
if ($DocId.Trim().Length -gt 0) {
    $argsList += @("--doc-id", $DocId)
}
if ($EnablePaddleOCR) {
    $argsList += "--enable-paddleocr"
}
if ($EnableHyperExtract) {
    $argsList += "--enable-hyper-extract"
}
if ($EnableApodex) {
    $argsList += "--enable-apodex"
}

python -m baxter_rag.cli @argsList
