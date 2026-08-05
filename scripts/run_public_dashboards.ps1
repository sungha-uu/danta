param(
    [switch]$NoPublish
)

$ErrorActionPreference = "Stop"
$now = Get-Date
$minuteOfDay = ($now.Hour * 60) + $now.Minute
if ($now.DayOfWeek -in @('Saturday', 'Sunday') -or $minuteOfDay -lt 525 -or $minuteOfDay -gt 960) {
    exit 0
}
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$logRoot = Join-Path $projectRoot "logs"
New-Item -ItemType Directory -Force -Path $logRoot | Out-Null
$log = Join-Path $logRoot "public-dashboards.log"

Set-Location $projectRoot
$arguments = @("-m", "danta.cli", "public-dashboards")
if (-not $NoPublish) {
    $arguments += "--publish"
}

try {
    & $python @arguments *>> $log
    if ($LASTEXITCODE -ne 0) {
        throw "public dashboard command exited with $LASTEXITCODE"
    }
}
catch {
    "[$(Get-Date -Format o)] FAILED: $($_.Exception.Message)" | Out-File -Append -Encoding utf8 $log
    exit 1
}
