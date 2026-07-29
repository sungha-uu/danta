$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$LogRoot = Join-Path $ProjectRoot "logs"
New-Item -ItemType Directory -Path $LogRoot -Force | Out-Null

Push-Location $ProjectRoot
try {
    & $Python -m danta.cli market-monitor *>> (Join-Path $LogRoot "market-monitor.log")
    if ($LASTEXITCODE -ne 0) {
        throw "Danta market monitor failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}
