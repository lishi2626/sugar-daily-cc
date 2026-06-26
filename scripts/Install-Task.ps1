<#
.SYNOPSIS
    Install or uninstall Sugar Daily scheduled tasks.
.DESCRIPTION
    Installs two local Windows scheduled tasks:
    - SugarDailyReport: 05:40 daily, generate report and publish to Vercel.
    - SugarDashboardMarketPerformance: 16:00 daily, independently fetch dashboard
      market performance using the same rules as Sugar Daily and publish to Vercel.
#>
param([switch]$Uninstall)

$ErrorActionPreference = "Stop"

$TaskName = "SugarDailyReport"
$DashboardTaskName = "SugarDashboardMarketPerformance"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ScriptPath = Join-Path $PSScriptRoot "Run-Daily.ps1"
$DashboardScriptPath = Join-Path $PSScriptRoot "Update-SugarDashboardMarket.ps1"
$LogDir = Join-Path $ProjectRoot "outputs"

if (-not (Test-Path -LiteralPath $LogDir)) {
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
}

if ($Uninstall) {
    foreach ($name in @($TaskName, $DashboardTaskName)) {
        try {
            Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction Stop
            Write-Host "Uninstalled: $name" -ForegroundColor Green
        } catch {
            Write-Host "Not found: $name" -ForegroundColor Yellow
        }
    }
    exit 0
}

Write-Host "Installing Sugar Daily scheduled tasks..."

$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 90) -MultipleInstances IgnoreNew
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

$ReportAction = New-ScheduledTaskAction -Execute "PowerShell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ScriptPath`""
$ReportTrigger = New-ScheduledTaskTrigger -Daily -At "05:40" -DaysInterval 1

$DashboardAction = New-ScheduledTaskAction -Execute "PowerShell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$DashboardScriptPath`""
$DashboardTrigger = New-ScheduledTaskTrigger -Daily -At "16:00" -DaysInterval 1

try {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $DashboardTaskName -Confirm:$false -ErrorAction SilentlyContinue

    Register-ScheduledTask -TaskName $TaskName -Trigger $ReportTrigger -Action $ReportAction -Settings $Settings `
        -Principal $Principal -Description "Sugar Daily Report - 05:40 daily, generate, verify, repair and publish to Vercel" `
        -Force -ErrorAction Stop

    Register-ScheduledTask -TaskName $DashboardTaskName -Trigger $DashboardTrigger -Action $DashboardAction -Settings $Settings `
        -Principal $Principal -Description "Sugar dashboard market performance - 16:00 daily independent refresh and Vercel publish" `
        -Force -ErrorAction Stop

    Write-Host "Installed: $TaskName at 05:40 daily" -ForegroundColor Green
    Write-Host "Installed: $DashboardTaskName at 16:00 daily" -ForegroundColor Green
    Write-Host "Manual dashboard refresh: PowerShell -ExecutionPolicy Bypass -File scripts\Update-SugarDashboardMarket.ps1"
    Write-Host "Uninstall: PowerShell -ExecutionPolicy Bypass -File scripts\Install-Task.ps1 -Uninstall"
} catch {
    Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
