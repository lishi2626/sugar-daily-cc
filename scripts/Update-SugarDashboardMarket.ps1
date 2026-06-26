# Update-SugarDashboardMarket.ps1
# Runs the independent 16:00 sugar dashboard market-performance refresh.
# It reuses Sugar Daily market-performance rules through scripts/market_performance.py
# but does not read generated Sugar Daily report output as a data source.

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$DashboardRoot = Join-Path (Split-Path -Parent $ProjectRoot) "nutstore_database_sync"
$DashboardScript = Join-Path $DashboardRoot "scripts\build_sugar_dashboard.py"
$DashboardData = Join-Path $ProjectRoot "public\dashboard\sugar_basis_dashboard_data.json"
$DashboardHtml = Join-Path $ProjectRoot "public\dashboard\sugar_basis_dashboard.html"
$VercelBaseUrl = "https://sugar-daily-cc.vercel.app"
$LogDir = Join-Path $ProjectRoot "outputs"
$TaskLog = Join-Path $LogDir ("dashboard_market_" + (Get-Date -Format "yyyyMMdd") + ".log")

function Write-Step {
    param([string]$Message)
    Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $Message"
}

function Test-MarketPerformancePayload {
    param([object]$Payload)

    if (-not $Payload.marketPerformance) { throw "marketPerformance is missing" }
    $mp = $Payload.marketPerformance
    if (-not $mp.fetchTime) { throw "marketPerformance.fetchTime is missing" }
    if (-not $mp.dataDate) { throw "marketPerformance.dataDate is missing" }
    if ($mp.ruleSource -ne "same_as_sugar_daily_market_performance") {
        throw "marketPerformance.ruleSource mismatch: $($mp.ruleSource)"
    }
    if ($mp.items.Count -ne 3) {
        throw "marketPerformance must contain 3 items, got $($mp.items.Count)"
    }
    foreach ($item in $mp.items) {
        if ($null -eq $item.value -or -not $item.unit) {
            throw "marketPerformance item is incomplete: $($item.name)"
        }
    }
}

function Test-VercelDashboard {
    $dataUrl = "$VercelBaseUrl/public/dashboard/sugar_basis_dashboard_data.json"
    $htmlUrl = "$VercelBaseUrl/public/dashboard/sugar_basis_dashboard.html"
    Write-Step "Verify Vercel dashboard: $dataUrl"
    for ($i = 1; $i -le 18; $i++) {
        try {
            $dataResp = Invoke-WebRequest -UseBasicParsing -Uri $dataUrl -TimeoutSec 20
            $htmlResp = Invoke-WebRequest -UseBasicParsing -Uri $htmlUrl -TimeoutSec 20
            $payload = $dataResp.Content | ConvertFrom-Json
            Test-MarketPerformancePayload -Payload $payload
            if ($dataResp.StatusCode -eq 200 -and $htmlResp.StatusCode -eq 200) {
                Write-Step "Vercel dashboard verified: fetchTime=$($payload.marketPerformance.fetchTime), dataDate=$($payload.marketPerformance.dataDate)"
                return
            }
        } catch {
            Write-Step "Vercel dashboard not ready yet (attempt $i/18): $($_.Exception.Message)"
        }
        Start-Sleep -Seconds 20
    }
    throw "Vercel dashboard verification failed after retry window."
}

if (-not (Test-Path -LiteralPath $LogDir)) {
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
}
Start-Transcript -Path $TaskLog -Append | Out-Null

try {
    Set-Location $ProjectRoot
    $env:PYTHONIOENCODING = "utf-8"
    if (-not (Test-Path -LiteralPath $DashboardScript)) {
        throw "Dashboard build script not found: $DashboardScript"
    }

    $DashboardVenvPython = Join-Path $DashboardRoot ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $DashboardVenvPython) {
        $DashboardPython = $DashboardVenvPython
    } else {
        $DashboardPython = "python"
    }

    Write-Step "Build sugar dashboard market performance independently"
    Push-Location $DashboardRoot
    try {
        & $DashboardPython scripts/build_sugar_dashboard.py
        if ($LASTEXITCODE -ne 0) {
            throw "build_sugar_dashboard.py failed with exit code $LASTEXITCODE"
        }
    } finally {
        Pop-Location
    }

    if (-not (Test-Path -LiteralPath $DashboardHtml)) { throw "Dashboard HTML not generated: $DashboardHtml" }
    if (-not (Test-Path -LiteralPath $DashboardData)) { throw "Dashboard data not generated: $DashboardData" }
    $payload = Get-Content -LiteralPath $DashboardData -Raw -Encoding UTF8 | ConvertFrom-Json
    Test-MarketPerformancePayload -Payload $payload
    Write-Step "Local dashboard verified: fetchTime=$($payload.marketPerformance.fetchTime), dataDate=$($payload.marketPerformance.dataDate)"

    $status = git status --porcelain public/dashboard
    $ahead = git status -sb | Select-String -Pattern "\[ahead [0-9]+\]"
    if ($status) {
        git add public/dashboard
        git commit -m ("Update sugar dashboard market performance " + (Get-Date -Format "yyyy-MM-dd"))
        if ($LASTEXITCODE -ne 0) { throw "git commit failed" }
    } elseif (-not $ahead) {
        Write-Step "No dashboard changes and no unpushed commits."
        return
    }

    $pushOk = $false
    for ($i = 1; $i -le 3; $i++) {
        Write-Step "git push attempt $i/3"
        git push
        if ($LASTEXITCODE -eq 0) {
            $pushOk = $true
            break
        }
        Start-Sleep -Seconds (10 * $i)
    }
    if (-not $pushOk) { throw "git push failed after 3 attempts; Vercel will not update until push succeeds." }

    Test-VercelDashboard
    Write-Step "Sugar dashboard market-performance update complete."
} catch {
    Write-Error $_.Exception.Message
    exit 1
} finally {
    Stop-Transcript | Out-Null
}
