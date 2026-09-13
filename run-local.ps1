# 本地手动跑一次签到（PowerShell）
# 用法： .\run-local.ps1                 # 从交互式输入读取密码
#       .\run-local.ps1 -Email a@b.com   # 指定邮箱
#       .\run-local.ps1 -NotifyOn always # 强制推送通知
param(
    [string]$Email = "sifangzhiji@qq.com",
    [string]$BaseUrl = "https://apix.chat",
    [ValidateSet("always", "on_failure", "on_success")]
    [string]$NotifyOn = "on_failure",
    [SecureString]$PasswordSecure
)

if ($PasswordSecure) {
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($PasswordSecure)
    $env:APIX_PASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
} else {
    $env:APIX_PASSWORD = Read-Host "apix.chat 登录密码"
}

$env:APIX_EMAIL = $Email
$env:APIX_BASE_URL = $BaseUrl
$env:APIX_NOTIFY_ON = $NotifyOn
$OutputEncoding = [Console]::OutputEncoding = [Text.UTF8Encoding]::new()

python "$PSScriptRoot\apix_checkin.py"
$code = $LASTEXITCODE

Remove-Item Env:APIX_PASSWORD, Env:APIX_EMAIL -ErrorAction SilentlyContinue
exit $code
