# 在 Windows 工作排程器註冊每日工作（需以系統管理員身分執行 PowerShell）。
#
#   KoushiDailyAutomation  每天 -AutomationTime 執行 run-automation.ps1（寄補件信、附件4、催件、讀教授回信）
#   KoushiDailyBackup      每天 -BackupTime 把正式資料備份到 -BackupDir
#   KoushiWeb（選用）      開機時啟動網頁（-IncludeWeb）
#
# 注意：系統只會在「期限設定」頁的每日發送時刻（預設 9 點）之後真的寄信；
# 排程時間早於發送時刻時，那一次只會預覽。所以 -AutomationTime 要晚於發送時刻。
param(
    [string]$AutomationTime = '09:30',
    [string]$BackupTime = '22:00',
    [Parameter(Mandatory = $true)][string]$BackupDir,
    [switch]$IncludeWeb
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\_common.ps1"
$python = Get-Python

function Register-Daily([string]$Name, [string]$Time, [string]$Command) {
    # /RU SYSTEM：不論是否有人登入都執行；/RL HIGHEST：以最高權限執行
    schtasks /Create /F /TN $Name /SC DAILY /ST $Time /RU SYSTEM /RL HIGHEST /TR $Command | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "註冊 $Name 失敗（請確認以系統管理員身分執行）" }
    Write-Host "已註冊 $Name，每天 $Time"
}

$ps = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File'
Register-Daily 'KoushiDailyAutomation' $AutomationTime "$ps `"$PSScriptRoot\run-automation.ps1`""
Register-Daily 'KoushiDailyBackup' $BackupTime "$ps `"$PSScriptRoot\run-backup.ps1`" -Dest `"$BackupDir`""

if ($IncludeWeb) {
    schtasks /Create /F /TN 'KoushiWeb' /SC ONSTART /RU SYSTEM /RL HIGHEST /TR "$ps `"$PSScriptRoot\start-web.ps1`"" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw '註冊 KoushiWeb 失敗' }
    Write-Host '已註冊 KoushiWeb，開機時啟動網頁'
}

Write-Host ''
Write-Host '建議立刻手動跑一次確認設定正確：'
Write-Host '  schtasks /Run /TN KoushiDailyAutomation'
Write-Host '  然後查看 outputs\_logs\ 底下當天的 automate-*.log'
