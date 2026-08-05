param()

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Executable = Join-Path $ProjectRoot ".venv\Scripts\danta.exe"
$LogRoot = Join-Path $ProjectRoot "data\live-daily-close\logs"
$Timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$LogPath = Join-Path $LogRoot "$Timestamp.log"

New-Item -ItemType Directory -Force -Path $LogRoot | Out-Null
if (-not (Test-Path -LiteralPath $Executable)) {
    throw "Danta executable not found: $Executable"
}

Push-Location $ProjectRoot
try {
    & $Executable daily-close *>&1 | Tee-Object -FilePath $LogPath
    if ($LASTEXITCODE -ne 0) {
        throw "Danta daily close failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}
