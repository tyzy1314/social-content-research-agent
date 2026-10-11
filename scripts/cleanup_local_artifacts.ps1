param(
    [switch]$Apply
)

$ErrorActionPreference = "Stop"

$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$targets = @()

$targets += Get-ChildItem -LiteralPath $root -Recurse -Force -Directory -Filter "__pycache__" |
    Where-Object { $_.FullName -notlike "$root\.git*" }

foreach ($name in @(".pytest_cache", ".ruff_cache")) {
    $path = Join-Path $root $name
    if (Test-Path -LiteralPath $path) {
        $targets += Get-Item -LiteralPath $path -Force
    }
}

$targets += Get-ChildItem -LiteralPath $root -Force -File -Filter "debug_*.html"

if (-not $targets) {
    Write-Host "No local artifacts found."
    exit 0
}

foreach ($target in $targets) {
    $resolved = (Resolve-Path -LiteralPath $target.FullName).Path
    if (-not $resolved.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove outside workspace: $resolved"
    }

    if ($Apply) {
        Remove-Item -LiteralPath $resolved -Recurse -Force
        Write-Host "removed: $resolved"
    } else {
        Write-Host "would remove: $resolved"
    }
}

if (-not $Apply) {
    Write-Host ""
    Write-Host "Dry run only. Re-run with -Apply to remove these artifacts."
}
