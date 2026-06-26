<#
.SYNOPSIS
    Sugar Daily Report daily runner.
.DESCRIPTION
    Intended for Windows Task Scheduler. Runs scripts/run_daily.py with the
    project virtual environment when available.
#>

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
$TargetDate = Get-Date -Format "yyyy-MM-dd"
$CompactDate = Get-Date -Format "yyyyMMdd"
$ReportDir = Join-Path $ProjectRoot "outputs\$TargetDate"
$ReportJson = Join-Path $ProjectRoot "public\data\reports\$TargetDate.json"
$IndexJson = Join-Path $ProjectRoot "public\data\reports.json"
$TaskLog = Join-Path $ProjectRoot "outputs\task_$CompactDate.log"
$VercelBaseUrl = "https://sugar-daily-cc.vercel.app"
$script:ReportRunExitCode = 1

function Write-Step {
    param([string]$Message)
    Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $Message"
}

function Invoke-ReportRun {
    param(
        [string]$PythonExe,
        [string]$PythonScript,
        [string]$Date
    )

    Write-Step "Run report generator for $Date"
    & $PythonExe $PythonScript --date $Date
    $script:ReportRunExitCode = $LASTEXITCODE
}

function Test-TodayArtifacts {
    param([switch]$ThrowOnMissing)

    $missing = @()
    $reportFile = $null
    if (Test-Path -LiteralPath $ReportDir) {
        $reportFile = Get-ChildItem -LiteralPath $ReportDir -Filter "*_$CompactDate.md" -File -ErrorAction SilentlyContinue | Select-Object -First 1
    }
    if (-not $reportFile) { $missing += (Join-Path $ReportDir "*_$CompactDate.md") }
    if (-not (Test-Path -LiteralPath $ReportJson)) { $missing += $ReportJson }
    if (-not (Test-Path -LiteralPath $IndexJson)) { $missing += $IndexJson }

    if ($missing.Count -gt 0) {
        $msg = "Missing daily artifacts: " + ($missing -join "; ")
        if ($ThrowOnMissing) { throw $msg }
        Write-Step $msg
        return $false
    }

    Write-Step "Daily artifacts exist: $($reportFile.FullName) ; $ReportJson"
    return $true
}

function Invoke-WebPublish {
    $publishScript = Join-Path $ProjectRoot "scripts\Publish-Web.ps1"
    if (-not (Test-Path -LiteralPath $publishScript)) {
        throw "Publish script not found: $publishScript"
    }

    Write-Step "Publish public data to GitHub/Vercel workflow"
    & PowerShell.exe -NoProfile -ExecutionPolicy Bypass -File $publishScript
    if ($LASTEXITCODE -ne 0) {
        throw "Publish-Web.ps1 failed with exit code $LASTEXITCODE"
    }
}

function Test-VercelReport {
    param([string]$Date)

    $reportUrl = "$VercelBaseUrl/public/data/reports/$Date.json"
    $indexUrl = "$VercelBaseUrl/public/data/reports.json"
    Write-Step "Verify Vercel report: $reportUrl"

    for ($i = 1; $i -le 18; $i++) {
        try {
            $reportResp = Invoke-WebRequest -UseBasicParsing -Uri $reportUrl -TimeoutSec 20
            $indexResp = Invoke-WebRequest -UseBasicParsing -Uri $indexUrl -TimeoutSec 20
            $indexJson = $indexResp.Content | ConvertFrom-Json
            $latestDate = $indexJson.reports[0].date
            if ($reportResp.StatusCode -eq 200 -and $indexResp.StatusCode -eq 200 -and $latestDate -eq $Date) {
                Write-Step "Vercel is synced: latest report=$latestDate ; $reportUrl"
                return $true
            }
            Write-Step "Vercel not synced yet (attempt $i/18): latest report=$latestDate"
        } catch {
            Write-Step "Vercel not ready yet (attempt $i/18): $($_.Exception.Message)"
        }
        Start-Sleep -Seconds 20
    }

    throw "Vercel verification failed: $Date is not live in reports.json and report JSON after retry window."
}

function Test-VercelDashboard {
    param([string]$Date)

    $dataUrl = "$VercelBaseUrl/public/dashboard/sugar_basis_dashboard_data.json"
    $htmlUrl = "$VercelBaseUrl/public/dashboard/sugar_basis_dashboard.html"
    Write-Step "Verify Vercel sugar dashboard market performance: $dataUrl"

    for ($i = 1; $i -le 18; $i++) {
        try {
            $dataResp = Invoke-WebRequest -UseBasicParsing -Uri $dataUrl -TimeoutSec 20
            $htmlResp = Invoke-WebRequest -UseBasicParsing -Uri $htmlUrl -TimeoutSec 20
            $payload = $dataResp.Content | ConvertFrom-Json
            $itemCount = $payload.marketPerformance.items.Count
            $reportDate = $payload.marketPerformance.reportDate
            if ($dataResp.StatusCode -eq 200 -and $htmlResp.StatusCode -eq 200 -and $reportDate -eq $Date -and $itemCount -eq 3) {
                Write-Step "Vercel dashboard is synced: marketPerformance.reportDate=$reportDate ; items=$itemCount"
                return $true
            }
            Write-Step "Vercel dashboard not synced yet (attempt $i/18): reportDate=$reportDate ; items=$itemCount"
        } catch {
            Write-Step "Vercel dashboard not ready yet (attempt $i/18): $($_.Exception.Message)"
        }
        Start-Sleep -Seconds 20
    }

    throw "Vercel dashboard verification failed: marketPerformance for $Date is not live after retry window."
}

if (-not (Test-Path -LiteralPath (Split-Path -Parent $TaskLog))) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $TaskLog) | Out-Null
}
Start-Transcript -Path $TaskLog -Append | Out-Null

Write-Host "=== Task started: $Timestamp ==="

try {
    Set-Location $ProjectRoot
    $env:PYTHONIOENCODING = "utf-8"

    $VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    if (Test-Path $VenvPython) {
        $PythonExe = $VenvPython
    } else {
        $PythonExe = "python"
    }

    $PythonScript = Join-Path $ProjectRoot "scripts\run_daily.py"
    Invoke-ReportRun -PythonExe $PythonExe -PythonScript $PythonScript -Date $TargetDate
    $exitCode = $script:ReportRunExitCode

    if ($exitCode -eq 0) {
        Write-Host "Daily report generated successfully (exit: 0)"
    } else {
        Write-Step "Daily report generation failed (exit: $exitCode); will check artifacts and retry once if needed."
    }

    if (-not (Test-TodayArtifacts)) {
        Write-Step "Attempt repair: rerun generator once for $TargetDate"
        Invoke-ReportRun -PythonExe $PythonExe -PythonScript $PythonScript -Date $TargetDate
        $retryExitCode = $script:ReportRunExitCode
        if ($retryExitCode -ne 0) {
            Write-Step "Repair rerun failed (exit: $retryExitCode)"
        }
        Test-TodayArtifacts -ThrowOnMissing | Out-Null
    }

    Invoke-WebPublish
    Test-VercelReport -Date $TargetDate | Out-Null
    Test-VercelDashboard -Date $TargetDate | Out-Null
    Write-Step "Daily workflow complete: report and dashboard synced to Vercel for $TargetDate"
}
catch {
    Write-Error $_.Exception.Message
    exit 1
}
finally {
    Stop-Transcript | Out-Null
}

Write-Host "=== Task finished: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ==="
