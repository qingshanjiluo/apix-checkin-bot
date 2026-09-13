# 本地跑一次「多站点签到 + 余额巡检」
# 用法： .\run-local.ps1                      # 读 sites.local.json（不存在则交互式录入）
#       .\run-local.ps1 -DryRun              # 只查询不签到
#       .\run-local.ps1 -Site xinsuanai      # 只跑名字/域名含该关键字的站
#       .\run-local.ps1 -NotifyOn always     # 强制推送通知
param(
    [string]$ConfigFile = "$PSScriptRoot\sites.local.json",
    [string]$Site = "",
    [switch]$DryRun,
    [ValidateSet("always", "on_failure", "on_success")]
    [string]$NotifyOn = "on_failure"
)

$OutputEncoding = [Console]::OutputEncoding = [Text.UTF8Encoding]::new()
$env:CHECKIN_NOTIFY_ON = $NotifyOn
if ($DryRun) { $env:CHECKIN_DRY_RUN = "1" }

if (-not (Test-Path $ConfigFile)) {
    Write-Host "未找到 $ConfigFile，改为交互式录入单站（仅 apix.chat 模式）" -ForegroundColor Yellow
    $env:APIX_EMAIL = if ($Site) { $Site } else { Read-Host "登录邮箱/用户名" }
    $env:APIX_PASSWORD = Read-Host "登录密码"
    $env:APIX_BASE_URL = Read-Host "站点地址（默认 https://apix.chat）"
    if (-not $env:APIX_BASE_URL) { $env:APIX_BASE_URL = "https://apix.chat" }
    python "$PSScriptRoot\checkin.py"
} else {
    python "$PSScriptRoot\checkin.py" --config "$ConfigFile" --site $Site
}

$code = $LASTEXITCODE
Remove-Item Env:APIX_PASSWORD, Env:APIX_EMAIL, Env:APIX_BASE_URL -ErrorAction SilentlyContinue
exit $code
