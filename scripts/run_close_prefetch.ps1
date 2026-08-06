param(
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Executable = Join-Path $ProjectRoot ".venv\Scripts\danta.exe"
$LogRoot = Join-Path $ProjectRoot "data\daily-runs"
$Timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$LogPath = Join-Path $LogRoot "$Timestamp-close-prefetch.log"

New-Item -ItemType Directory -Force -Path $LogRoot | Out-Null
if (-not (Test-Path -LiteralPath $Executable)) {
    throw "Danta executable not found: $Executable"
}

$Arguments = @("close-prefetch")
if ($Force) {
    $Arguments += "--force"
}

Push-Location $ProjectRoot
try {
    & $Executable @Arguments *>&1 | Tee-Object -FilePath $LogPath
    if ($LASTEXITCODE -ne 0) {
        throw "Danta close prefetch failed with exit code $LASTEXITCODE"
    }
}
catch {
    $FailurePath = Join-Path $LogRoot "$Timestamp-close-prefetch-failure.json"
    @{
        failed_at = (Get-Date).ToString("o")
        command = "close-prefetch"
        error = $_.Exception.Message
        log_path = $LogPath
    } | ConvertTo-Json | Set-Content -LiteralPath $FailurePath -Encoding utf8
    throw
}
finally {
    Pop-Location
}
