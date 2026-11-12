# 部署腳本共用：專案根目錄、讀 .env 進目前程序的環境變數、找 Python。
# 由其他 .ps1 以「. "$PSScriptRoot\_common.ps1"」載入，不單獨執行。

$ProjectRoot = Split-Path -Parent $PSScriptRoot

function Import-DotEnv {
    # 專案根目錄的 .env（已在 .gitignore）：一行一個 KEY=VALUE，# 開頭為註解。
    # 只設定到目前這個 PowerShell 程序，不寫進系統環境變數。
    $path = Join-Path $ProjectRoot '.env'
    if (-not (Test-Path $path)) {
        Write-Warning "找不到 $path，改用系統環境變數"
        return
    }
    foreach ($line in [System.IO.File]::ReadAllLines($path, [System.Text.Encoding]::UTF8)) {
        $text = $line.Trim()
        if ($text -eq '' -or $text.StartsWith('#') -or -not $text.Contains('=')) { continue }
        $index = $text.IndexOf('=')
        $key = $text.Substring(0, $index).Trim()
        $value = $text.Substring($index + 1).Trim().Trim('"')
        [Environment]::SetEnvironmentVariable($key, $value, 'Process')
    }
}

function Get-Python {
    $venv = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    if (Test-Path $venv) { return $venv }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($null -eq $cmd) { throw '找不到 Python：請先依操作手冊建立 .venv' }
    return $cmd.Source
}

function Write-Utf8NoBom([string]$Path, [string]$Text, [switch]$Append) {
    # PowerShell 5.1 的 Out-File／Set-Content 會加 BOM，Python 讀 JSON 時會失敗。
    $encoding = New-Object System.Text.UTF8Encoding($false)
    if ($Append) { [System.IO.File]::AppendAllText($Path, $Text, $encoding) }
    else { [System.IO.File]::WriteAllText($Path, $Text, $encoding) }
}
