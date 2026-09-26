# Runs the screener and restarts it if it ever exits (crash, MT5 restart, ...).
# Extra arguments are passed to wednesday, e.g. .\scripts\run_wednesday.ps1 --lookback 300
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) { $uv = Join-Path $env:USERPROFILE ".local\bin\uv.exe" }
if (-not (Test-Path $uv)) { throw "uv not found. Install it: powershell -c `"irm https://astral.sh/uv/install.ps1 | iex`"" }

while ($true) {
    & $uv run --extra mt5 --extra llm wednesday @args
    Write-Host "$(Get-Date -Format s) wednesday exited with code $LASTEXITCODE, restarting in 30s"
    Start-Sleep -Seconds 30
}
