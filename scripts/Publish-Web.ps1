# Publish-Web.ps1
# Updates public report JSON and pushes changes to GitHub to trigger Vercel.
# Usage: PowerShell -ExecutionPolicy Bypass -File scripts\Publish-Web.ps1

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot
$env:PYTHONIOENCODING = "utf-8"
$DashboardRoot = Join-Path (Split-Path -Parent $ProjectRoot) "nutstore_database_sync"
$DashboardData = Join-Path $ProjectRoot "public\dashboard\sugar_basis_dashboard_data.json"
$DashboardHtml = Join-Path $ProjectRoot "public\dashboard\sugar_basis_dashboard.html"

$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (Test-Path $VenvPython) {
    $PythonExe = $VenvPython
} else {
    $PythonExe = "python"
}

Write-Host "=== Updating frontend JSON ===" -ForegroundColor Cyan
& $PythonExe scripts/update_web_reports.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "update_web_reports.py failed; stopping." -ForegroundColor Red
    exit 1
}

Write-Host "`n=== Building sugar dashboard ===" -ForegroundColor Cyan
$DashboardScript = Join-Path $DashboardRoot "scripts\build_sugar_dashboard.py"
if (-not (Test-Path -LiteralPath $DashboardScript)) {
    Write-Host "Dashboard build script not found: $DashboardScript" -ForegroundColor Red
    exit 1
}
$DashboardVenvPython = Join-Path $DashboardRoot ".venv\Scripts\python.exe"
if (Test-Path -LiteralPath $DashboardVenvPython) {
    $DashboardPython = $DashboardVenvPython
} else {
    $DashboardPython = "python"
}
Push-Location $DashboardRoot
try {
    & $DashboardPython scripts/build_sugar_dashboard.py
    if ($LASTEXITCODE -ne 0) {
        Write-Host "build_sugar_dashboard.py failed; stopping." -ForegroundColor Red
        exit 1
    }
} finally {
    Pop-Location
}
if (-not (Test-Path -LiteralPath $DashboardHtml)) {
    Write-Host "Dashboard HTML not generated: $DashboardHtml" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path -LiteralPath $DashboardData)) {
    Write-Host "Dashboard data not generated: $DashboardData" -ForegroundColor Red
    exit 1
}
$dashboardPayload = Get-Content -LiteralPath $DashboardData -Raw -Encoding UTF8 | ConvertFrom-Json
$date = Get-Date -Format "yyyy-MM-dd"
if (-not $dashboardPayload.marketPerformance) {
    Write-Host "Dashboard data missing marketPerformance." -ForegroundColor Red
    exit 1
}
if (-not $dashboardPayload.marketPerformance.fetchTime) {
    Write-Host "Dashboard marketPerformance missing fetchTime." -ForegroundColor Red
    exit 1
}
if (-not $dashboardPayload.marketPerformance.dataDate) {
    Write-Host "Dashboard marketPerformance missing dataDate." -ForegroundColor Red
    exit 1
}
if ($dashboardPayload.marketPerformance.ruleSource -ne "same_as_sugar_daily_market_performance") {
    Write-Host "Dashboard marketPerformance ruleSource mismatch: $($dashboardPayload.marketPerformance.ruleSource)" -ForegroundColor Red
    exit 1
}
if ($dashboardPayload.marketPerformance.items.Count -ne 3) {
    Write-Host "Dashboard marketPerformance must contain 3 items, got $($dashboardPayload.marketPerformance.items.Count)" -ForegroundColor Red
    exit 1
}
foreach ($item in $dashboardPayload.marketPerformance.items) {
    if ($null -eq $item.value -or -not $item.unit) {
        Write-Host "Dashboard marketPerformance item is incomplete: $($item.name)" -ForegroundColor Red
        exit 1
    }
}

Write-Host "`n=== Checking Git status ===" -ForegroundColor Cyan
$status = git status --porcelain
$ahead = git status -sb | Select-String -Pattern "\[ahead [0-9]+\]"
if (-not $status) {
    if ($ahead) {
        Write-Host "No file changes, but local branch has unpushed commits. Will push existing commits." -ForegroundColor Yellow
    } else {
        Write-Host "No file changes and no unpushed commits." -ForegroundColor Yellow
        exit 0
    }
}

Write-Host "`n=== Committing and pushing ===" -ForegroundColor Cyan
if ($status) {
    git add index.html public .gitignore
    git commit -m "Update sugar daily report $date"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "git commit failed." -ForegroundColor Red
        exit 1
    }
}

$pushOk = $false
for ($i = 1; $i -le 3; $i++) {
    Write-Host "git push attempt $i/3" -ForegroundColor Cyan
    git push
    if ($LASTEXITCODE -eq 0) {
        $pushOk = $true
        break
    }
    Start-Sleep -Seconds (10 * $i)
}

if (-not $pushOk) {
    Write-Host "git push failed after 3 attempts; Vercel will not update until push succeeds." -ForegroundColor Red
    exit 1
}

Write-Host "`n=== Done ===" -ForegroundColor Green
Write-Host "Pushed: Update sugar daily report $date"
Write-Host "Vercel should redeploy automatically."
