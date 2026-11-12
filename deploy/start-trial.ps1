param(
    [switch]$Check,
    [ValidateRange(1, 65535)][int]$Port = 5000
)
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    throw "請先建立 .venv 並安裝 requirements.txt"
}
$env:PYTHONUTF8 = "1"
$TrialArgs = @("-m", "koushi.trial", "--port", "$Port")
if ($Check) { $TrialArgs += "--check" }
& $Python @TrialArgs
exit $LASTEXITCODE
