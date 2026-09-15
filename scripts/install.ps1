# Thin wrapper around the cross-platform installer.
# Everything real lives in install.py so Windows/macOS/Linux stay in step.
$root = Split-Path $PSScriptRoot -Parent
$py = Get-Command python -ErrorAction SilentlyContinue
if ($null -eq $py) { $py = Get-Command python3 -ErrorAction SilentlyContinue }
if ($null -eq $py) {
    Write-Host "ERROR: python is not on PATH. Install Python 3.8+ and retry." -ForegroundColor Red
    exit 1
}
& $py.Source (Join-Path $root "install.py") @args
exit $LASTEXITCODE
