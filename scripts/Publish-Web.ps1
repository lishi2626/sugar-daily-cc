# Publish-Web.ps1
# 将日报 JSON 更新并推送到 GitHub，触发 Vercel 自动部署。
# 用法: PowerShell -ExecutionPolicy Bypass -File scripts\Publish-Web.ps1

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

Write-Host "=== 更新前端 JSON ===" -ForegroundColor Cyan
& py scripts/update_web_reports.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "update_web_reports.py 失败，终止。" -ForegroundColor Red
    exit 1
}

Write-Host "`n=== 检查 Git 状态 ===" -ForegroundColor Cyan
$status = git status --porcelain
if (-not $status) {
    Write-Host "没有文件变化，无需提交。" -ForegroundColor Yellow
    exit 0
}

Write-Host "`n=== 提交并推送 ===" -ForegroundColor Cyan
$date = Get-Date -Format "yyyy-MM-dd"
git add index.html public/data .gitignore
git commit -m "Update sugar daily report $date"
if ($LASTEXITCODE -ne 0) {
    Write-Host "git commit 失败。" -ForegroundColor Red
    exit 1
}

git push
if ($LASTEXITCODE -ne 0) {
    Write-Host "git push 失败，请检查网络和 Git 凭据。" -ForegroundColor Red
    exit 1
}

Write-Host "`n=== 完成 ===" -ForegroundColor Green
Write-Host "已推送: Update sugar daily report $date"
Write-Host "Vercel 将自动重新部署。"
