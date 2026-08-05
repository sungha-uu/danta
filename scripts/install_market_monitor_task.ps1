$ErrorActionPreference = "Stop"

# Market sensing is owned by the unified trading runtime. A second process uses
# a separate rate limiter and can exceed the KIS per-second transaction limit.
$legacyTaskName = "Danta-Market-Monitor-0850"
if (Get-ScheduledTask -TaskName $legacyTaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $legacyTaskName -Confirm:$false
    Write-Output "Removed legacy duplicate task: $legacyTaskName"
}
else {
    Write-Output "Legacy duplicate task is already absent"
}
