@echo off
REM Registers a hidden PowerShell launcher to run at every login.
REM Drops a shortcut in the user's Startup folder. No admin needed.

setlocal
set "PROJECT_DIR=%~dp0.."
for %%I in ("%PROJECT_DIR%") do set "PROJECT_DIR=%%~fI"
set "PS1=%PROJECT_DIR%\scripts\start_hidden.ps1"
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "LNK=%STARTUP%\YouLearn Uploader.lnk"

echo Creating shortcut: %LNK%
powershell -NoProfile -Command ^
  "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%LNK%');" ^
  "$s.TargetPath='powershell.exe';" ^
  "$s.Arguments='-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File ""%PS1%""';" ^
  "$s.WorkingDirectory='%PROJECT_DIR%';" ^
  "$s.WindowStyle=7;" ^
  "$s.Save()"

if errorlevel 1 (
  echo Failed.
  exit /b 1
)

echo Starting the server now (hidden)...
powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "%PS1%"

echo.
echo Done. The uploader will start automatically on every login.
echo Bookmark this URL: http://127.0.0.1:8765/
echo.
pause
