# Thin wrapper around the cross-platform installer.
$root = Split-Path $PSScriptRoot -Parent
$py = Get-Command python -ErrorAction SilentlyContinue
if ($null -eq $py) { $py = Get-Command python3 -ErrorAction SilentlyContinue }
if ($null -eq $py) {
    Write-Host "ERROR: python is not on PATH." -ForegroundColor Red
    exit 1
}
& $py.Source (Join-Path $root "install.py") --remove @args
exit $LASTEXITCODE
