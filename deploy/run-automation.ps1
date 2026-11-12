# 每日自動化：給 Windows 工作排程器呼叫（見 docs/部署與上線操作手冊.md）。
#
# 等同手動執行 `python -m koushi automate --send`，另外：
#   - 讀專案根目錄 .env（寄信帳號、KOUSHI_PUBLIC_URL 等）
#   - 輸出寫到 outputs\_logs\automate-日期.log（保留 90 天）
#   - Python 本身沒能啟動或中途當掉時，仍寫一筆失敗到 outputs\_state\automation_status.json，
#     系辦工作總覽頂端就會出現「每日自動化執行失敗」警示
param(
    [switch]$Preview  # 只預覽、不寄信（測試排程是否設定正確時用）
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\_common.ps1"
Import-DotEnv
$env:KOUSHI_ENV = 'production'
if (-not $env:KOUSHI_DATA_ROOT -or -not $env:KOUSHI_OUTPUT_ROOT) {
    throw '正式排程必須設定 KOUSHI_DATA_ROOT 與 KOUSHI_OUTPUT_ROOT，禁止使用示範目錄'
}
$logDir = Join-Path $env:KOUSHI_OUTPUT_ROOT '_logs'
$stateDir = Join-Path $env:KOUSHI_OUTPUT_ROOT '_state'
New-Item -ItemType Directory -Force -Path $logDir, $stateDir | Out-Null
$log = Join-Path $logDir ('automate-{0}.log' -f (Get-Date -Format 'yyyy-MM-dd'))
$statusPath = Join-Path $stateDir 'automation_status.json'
$started = Get-Date
Write-Utf8NoBom $log ("`r`n===== {0} 開始 =====`r`n" -f $started.ToString('yyyy-MM-dd HH:mm:ss')) -Append

$exitCode = 1
$failure = ''
try {
    Import-DotEnv
    $python = Get-Python
    $env:PYTHONIOENCODING = 'utf-8'
    $sendFlag = '--send'
    if ($Preview) { $sendFlag = '' }
    # 用 cmd 轉向輸出：PowerShell 5.1 直接轉向原生程式的 stderr 會被當成錯誤中斷
    Push-Location $ProjectRoot
    try {
        cmd /c "`"$python`" -m koushi automate $sendFlag >> `"$log`" 2>&1"
        $exitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }
} catch {
    $failure = $_.Exception.Message
    Write-Utf8NoBom $log ("啟動失敗：{0}`r`n" -f $failure) -Append
}

# Python 有跑到 cli 的 automate，就已自己寫入成功／失敗；這裡只補「根本沒跑起來」的情況。
$recorded = (Test-Path $statusPath) -and ((Get-Item $statusPath).LastWriteTime -ge $started)
if ($exitCode -ne 0 -and -not $recorded) {
    if ($failure -eq '') { $failure = "程式異常結束（結束代碼 $exitCode）" }
    $status = [ordered]@{
        at = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'); ok = $false; phase = ''
        error = "$failure；詳見 outputs\_logs\$(Split-Path -Leaf $log)"; notes = @()
    }
    Write-Utf8NoBom $statusPath ($status | ConvertTo-Json -Compress)
}

Write-Utf8NoBom $log ("===== {0} 結束，結束代碼 {1} =====`r`n" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $exitCode) -Append
Get-ChildItem $logDir -Filter 'automate-*.log' |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-90) } |
    Remove-Item -Force
exit $exitCode
