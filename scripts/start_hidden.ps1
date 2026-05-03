$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $projectDir ".venv\Scripts\python.exe"
$logsDir = Join-Path $projectDir "logs"
$launcherLog = Join-Path $logsDir "launcher.log"

New-Item -ItemType Directory -Force -Path $logsDir | Out-Null

if (-not (Test-Path $pythonExe)) {
    Add-Content -Path $launcherLog -Value "$(Get-Date -Format s) ERROR Missing venv Python launcher: $pythonExe"
    exit 1
}

Set-Location $projectDir
Add-Content -Path $launcherLog -Value "$(Get-Date -Format s) INFO Launch requested: $pythonExe -m uploader"
& $pythonExe -m uploader
