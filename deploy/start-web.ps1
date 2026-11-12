# 正式啟動網頁（waitress）。開發時仍可用 `python -m koushi.web.app`（Flask 開發伺服器）。
#
# waitress 本身不處理 HTTPS：預設只聽本機 127.0.0.1:5000，由前面的 HTTPS 反向代理
# （學校網管提供，或 IIS／Caddy 等）轉進來。委員專屬連結會帶個資，一定要走 HTTPS。
param(
    [string]$Listen = '127.0.0.1:5000'
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\_common.ps1"
Import-DotEnv
$env:KOUSHI_ENV = 'production'

$missing = @('KOUSHI_WEB_SECRET_KEY', 'KOUSHI_OFFICE_PASSWORD', 'KOUSHI_PUBLIC_URL', 'KOUSHI_DATA_ROOT', 'KOUSHI_OUTPUT_ROOT') |
    Where-Object { -not [Environment]::GetEnvironmentVariable($_, 'Process') }
if ($missing) {
    throw ('.env 缺少必設項目：' + ($missing -join '、') + '（見 deploy\.env.example）')
}
if (-not $env:KOUSHI_PUBLIC_URL.StartsWith('https://')) {
    throw "正式環境 KOUSHI_PUBLIC_URL 必須使用 HTTPS"
}

$python = Get-Python
Push-Location $ProjectRoot
try {
    Write-Host "口試系統網頁啟動於 http://$Listen（對外網址 $env:KOUSHI_PUBLIC_URL）；按 Ctrl+C 停止"
    & $python -m waitress "--listen=$Listen" --threads=8 koushi.web.app:app
} finally {
    Pop-Location
}
