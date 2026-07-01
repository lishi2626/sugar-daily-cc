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
$VercelBaseUrl = "https://sugar-daily-cc.vercel.app"

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

$MaxPushAttempts = 12
$pushOk = $false
for ($i = 1; $i -le $MaxPushAttempts; $i++) {
    Write-Host "git push attempt $i/$MaxPushAttempts" -ForegroundColor Cyan
    git push
    if ($LASTEXITCODE -eq 0) {
        $pushOk = $true
        break
    }
    if ($i -lt $MaxPushAttempts) {
        Start-Sleep -Seconds ([Math]::Min(300, 30 * $i))
    }
}

if (-not $pushOk) {
    Write-Host "git push failed after $MaxPushAttempts attempts; local commit is kept and Vercel will not update until push succeeds." -ForegroundColor Red
    exit 1
}

function Test-VercelReport {
    param([string]$Date)

    $reportUrl = "$VercelBaseUrl/public/data/reports/$Date.json"
    $indexUrl = "$VercelBaseUrl/public/data/reports.json"
    Write-Host "`n=== Verifying Vercel report ===" -ForegroundColor Cyan

    for ($i = 1; $i -le 18; $i++) {
        try {
            $reportResp = Invoke-WebRequest -UseBasicParsing -Uri $reportUrl -TimeoutSec 20
            $indexResp = Invoke-WebRequest -UseBasicParsing -Uri $indexUrl -TimeoutSec 20
            $indexPayload = $indexResp.Content | ConvertFrom-Json
            $latestDate = $indexPayload.reports[0].date
            if ($reportResp.StatusCode -eq 200 -and $indexResp.StatusCode -eq 200 -and $latestDate -eq $Date) {
                Write-Host "Vercel report verified: latest=$latestDate ; $reportUrl" -ForegroundColor Green
                return
            }
            Write-Host "Vercel report not synced yet (attempt $i/18): latest=$latestDate" -ForegroundColor Yellow
        } catch {
            Write-Host "Vercel report not ready yet (attempt $i/18): $($_.Exception.Message)" -ForegroundColor Yellow
        }
        Start-Sleep -Seconds 20
    }
    throw "Vercel report verification failed: $Date is not live after retry window."
}

function Test-VercelDashboard {
    $dataUrl = "$VercelBaseUrl/public/dashboard/sugar_basis_dashboard_data.json"
    $htmlUrl = "$VercelBaseUrl/public/dashboard/sugar_basis_dashboard.html"
    Write-Host "`n=== Verifying Vercel dashboard ===" -ForegroundColor Cyan

    for ($i = 1; $i -le 18; $i++) {
        try {
            $dataResp = Invoke-WebRequest -UseBasicParsing -Uri $dataUrl -TimeoutSec 20
            $htmlResp = Invoke-WebRequest -UseBasicParsing -Uri $htmlUrl -TimeoutSec 20
            $payload = $dataResp.Content | ConvertFrom-Json
            $itemCount = $payload.marketPerformance.items.Count
            $fetchTime = $payload.marketPerformance.fetchTime
            $dataDate = $payload.marketPerformance.dataDate
            $ruleSource = $payload.marketPerformance.ruleSource
            if ($dataResp.StatusCode -eq 200 -and $htmlResp.StatusCode -eq 200 -and $fetchTime -and $dataDate -and $ruleSource -eq "same_as_sugar_daily_market_performance" -and $itemCount -eq 3) {
                Write-Host "Vercel dashboard verified: fetchTime=$fetchTime ; dataDate=$dataDate ; items=$itemCount" -ForegroundColor Green
                return
            }
            Write-Host "Vercel dashboard not synced yet (attempt $i/18): fetchTime=$fetchTime ; dataDate=$dataDate ; items=$itemCount" -ForegroundColor Yellow
        } catch {
            Write-Host "Vercel dashboard not ready yet (attempt $i/18): $($_.Exception.Message)" -ForegroundColor Yellow
        }
        Start-Sleep -Seconds 20
    }
    throw "Vercel dashboard verification failed after retry window."
}

Test-VercelReport -Date $date
Test-VercelDashboard

Write-Host "`n=== Done ===" -ForegroundColor Green
Write-Host "Pushed: Update sugar daily report $date"
Write-Host "Vercel publish verified."
