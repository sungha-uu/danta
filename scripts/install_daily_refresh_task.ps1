param(
    [string]$TaskName = "Danta-Daily-1600",
    [string]$PrefetchTaskName = "Danta-Close-Prefetch-1531"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Runner = Join-Path $ProjectRoot "scripts\run_daily_refresh.ps1"
$PowerShell = (Get-Command powershell.exe).Source
$Action = New-ScheduledTaskAction `
    -Execute $PowerShell `
    -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$Runner`"" `
    -WorkingDirectory $ProjectRoot
$PrefetchAction = New-ScheduledTaskAction `
    -Execute $PowerShell `
    -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command `"Set-Location -LiteralPath '$ProjectRoot'; & '$ProjectRoot\.venv\Scripts\danta.exe' close-prefetch`"" `
    -WorkingDirectory $ProjectRoot

$Triggers = @(
    New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "16:00"
    New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "16:10"
    New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "16:20"
    New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "16:30"
)
$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 3)
$Principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Triggers `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Danta KOSPI daily refresh and GitHub Pages publish after market close" `
    -Force | Out-Null

$PrefetchTrigger = New-ScheduledTaskTrigger `
    -Weekly `
    -WeeksInterval 1 `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
    -At "15:31"
Register-ScheduledTask `
    -TaskName $PrefetchTaskName `
    -Action $PrefetchAction `
    -Trigger $PrefetchTrigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Prefetch Danta KOSPI minute bars after the regular close" `
    -Force | Out-Null

Get-ScheduledTask -TaskName $TaskName,$PrefetchTaskName |
    Select-Object TaskName, State, Description
