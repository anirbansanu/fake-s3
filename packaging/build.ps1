# Build both standalone Windows executables into dist\.
# Usage:  powershell -ExecutionPolicy Bypass -File packaging\build.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python" }

Write-Host "== generating icon =="
& $python (Join-Path $PSScriptRoot "make_icon.py")
if ($LASTEXITCODE -ne 0) { throw "icon generation failed" }

Write-Host "== building fakes3-cli.exe =="
& $python -m PyInstaller --noconfirm --distpath (Join-Path $root "dist") `
    --workpath (Join-Path $root "build") (Join-Path $PSScriptRoot "fakes3-cli.spec")
if ($LASTEXITCODE -ne 0) { throw "CLI build failed" }

Write-Host "== building fakes3-gui.exe =="
& $python -m PyInstaller --noconfirm --distpath (Join-Path $root "dist") `
    --workpath (Join-Path $root "build") (Join-Path $PSScriptRoot "fakes3-gui.spec")
if ($LASTEXITCODE -ne 0) { throw "GUI build failed" }

Write-Host "== done =="
Get-ChildItem (Join-Path $root "dist") -Filter "*.exe" |
    ForEach-Object { "{0}  {1:N1} MB" -f $_.Name, ($_.Length / 1MB) }
