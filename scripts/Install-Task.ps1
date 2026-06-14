<#
.SYNOPSIS
    SugarDailyReport — 每个工作日 05:40 执行，06:00 前完成
#>
param([switch]$Uninstall)

$TaskName = "SugarDailyReport"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ScriptPath = Join-Path $PSScriptRoot "Run-Daily.ps1"
$LogDir = Join-Path $ProjectRoot "outputs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Force -Path $LogDir | Out-Null }

if ($Uninstall) {
    try { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop; Write-Host "Uninstalled." -F Green }
    catch { Write-Host "Not found." -F Yellow }
    exit 0
}

Write-Host "SugarDailyReport — 05:40 daily, target 06:00"
$Action = New-ScheduledTaskAction -Execute "PowerShell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ScriptPath`""
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 20) -MultipleInstances IgnoreNew
$Trigger = New-ScheduledTaskTrigger -Daily -At "05:40" -DaysInterval 1
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive

try {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Register-ScheduledTask -TaskName $TaskName -Trigger $Trigger -Action $Action -Settings $Settings `
        -Principal $Principal -Description "Sugar Daily Report — 05:40, target 06:00" -Force -ErrorAction Stop
    Write-Host "Installed. Trigger: 05:40 weekdays" -F Green
    Write-Host "Uninstall: PowerShell -ExecutionPolicy Bypass -File scripts\Install-Task.ps1 -Uninstall"
} catch {
    Write-Host "ERROR: $($_.Exception.Message)" -F Red
    exit 1
}
