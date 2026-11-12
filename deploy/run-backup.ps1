param([Parameter(Mandatory=$true)][string]$Dest,[int]$KeepDays=30)
$ErrorActionPreference='Stop'
. "$PSScriptRoot\_common.ps1"
Import-DotEnv
$env:KOUSHI_ENV='production'
if (-not $env:KOUSHI_DATA_ROOT -or -not $env:KOUSHI_OUTPUT_ROOT) {
    throw '正式備份必須設定獨立資料與輸出目錄'
}
$python=Get-Python
& $python "$PSScriptRoot\backup_state.py" --dest $Dest --keep-days $KeepDays
exit $LASTEXITCODE
