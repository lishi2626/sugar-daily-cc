<#
.SYNOPSIS
    Build, publish and verify Sugar News dashboard.
#>

param(
    [string]$Date,
    [string]$TaskRoot,
    [switch]$SkipIfSuccess
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot
$env:PYTHONIOENCODING = "utf-8"
$env:TZ = "Asia/Shanghai"

if (-not $TaskRoot) {
    $TaskRoot = Join-Path (Split-Path -Parent $ProjectRoot) "Sugar News"
}

$VercelBaseUrl = "https://sugar-daily-cc.vercel.app"
$LogRoot = Join-Path $ProjectRoot "outputs"
if (-not (Test-Path -LiteralPath $LogRoot)) {
    New-Item -ItemType Directory -Force -Path $LogRoot | Out-Null
}
$LogDate = Get-Date -Format "yyyyMMdd"
$TaskLog = Join-Path $LogRoot "sugar_news_$LogDate.log"

function Write-Step {
    param([string]$Message)
    Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $Message"
}

function Get-PythonExe {
    $VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $VenvPython) { return $VenvPython }
    return "python"
}

function Get-BeijingYesterday {
    $script = @"
from datetime import datetime, timedelta
try:
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Asia/Shanghai")
except Exception:
    from datetime import timezone
    tz = timezone(timedelta(hours=8), name="Asia/Shanghai")
print((datetime.now(tz).date() - timedelta(days=1)).isoformat())
"@
    & (Get-PythonExe) -c $script
}

function Invoke-RemoteVerify {
    param([string]$TargetDate)
    for ($i = 1; $i -le 18; $i++) {
        try {
            & (Get-PythonExe) scripts/verify_sugar_news_dashboard.py --date $TargetDate --base-url $VercelBaseUrl
            if ($LASTEXITCODE -eq 0) { return }
        } catch {
            Write-Step "Vercel not ready yet (attempt $i/18): $($_.Exception.Message)"
        }
        Start-Sleep -Seconds 20
    }
    throw "Vercel Sugar News verification failed after retry window."
}

Start-Transcript -Path $TaskLog -Append | Out-Null
try {
    if (-not $Date) {
        $Date = Get-BeijingYesterday
    }

    Write-Step "Build Sugar News for $Date using task root: $TaskRoot"
    $args = @("scripts/sugar_news_pipeline.py", "--date", $Date, "--task-root", $TaskRoot)
    if ($SkipIfSuccess) { $args += "--skip-if-success" }
    & (Get-PythonExe) @args
    if ($LASTEXITCODE -ne 0) {
        throw "sugar_news_pipeline.py failed with exit code $LASTEXITCODE"
    }

    Write-Step "Verify local Sugar News dashboard data"
    & (Get-PythonExe) scripts/verify_sugar_news_dashboard.py --date $Date
    if ($LASTEXITCODE -ne 0) {
        throw "Local Sugar News dashboard verification failed"
    }

    Write-Step "Commit and push Sugar News changes"
    $status = git status --porcelain public/sugar-news
    $ahead = git status -sb | Select-String -Pattern "\[ahead [0-9]+\]"
    if ($status) {
        git add public/sugar-news
        git commit -m "Update Sugar News $Date"
        if ($LASTEXITCODE -ne 0) { throw "git commit failed" }
    }
    if ($status -or $ahead) {
        for ($i = 1; $i -le 12; $i++) {
            Write-Step "git push attempt $i/12"
            git push
            if ($LASTEXITCODE -eq 0) { break }
            if ($i -eq 12) { throw "git push failed after retries" }
            Start-Sleep -Seconds ([Math]::Min(300, 30 * $i))
        }
    } else {
        Write-Step "No Sugar News changes to push"
    }

    Write-Step "Verify Vercel Sugar News page"
    Invoke-RemoteVerify -TargetDate $Date
    Write-Step "Sugar News workflow complete for $Date"
}
finally {
    Stop-Transcript | Out-Null
}
