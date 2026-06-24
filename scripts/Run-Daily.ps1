<#
.SYNOPSIS
    白糖日报 - 每日执行脚本
.DESCRIPTION
    由 Windows 任务计划程序调用。
    激活虚拟环境后运行 run_daily.py。
    只生成当日日报，不写额外日志文件。
#>

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Write-Host "=== 任务触发: $Timestamp ==="

try {
    Set-Location $ProjectRoot

    # 激活虚拟环境
    $VenvActivate = Join-Path $ProjectRoot ".venv\Scripts\Activate.ps1"
    if (Test-Path $VenvActivate) {
        . $VenvActivate
    }

    # 运行主程序
    $PythonScript = Join-Path $ProjectRoot "scripts\run_daily.py"
    py $PythonScript

    $exitCode = $LASTEXITCODE
    if ($exitCode -eq 0) {
        Write-Host "日报生成成功 (exit: 0)"
    } else {
        Write-Host "日报生成失败 (exit: $exitCode)"
    }
}
catch {
    $errMsg = $_.Exception.Message
    Write-Error $errMsg
    exit 1
}

Write-Host "=== 任务结束: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ==="
