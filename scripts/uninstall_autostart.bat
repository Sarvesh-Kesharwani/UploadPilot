@echo off
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "LNK=%STARTUP%\YouLearn Uploader.lnk"
if exist "%LNK%" (
  del "%LNK%"
  echo Removed: %LNK%
) else (
  echo No shortcut found at %LNK%
)

echo Killing any running python.exe/pythonw.exe instances of the uploader...
for /f "tokens=2" %%P in ('tasklist /fi "imagename eq python.exe" /fo csv /nh 2^>nul') do (
  taskkill /PID %%~P /F >nul 2>&1
)
for /f "tokens=2" %%P in ('tasklist /fi "imagename eq pythonw.exe" /fo csv /nh 2^>nul') do (
  taskkill /PID %%~P /F >nul 2>&1
)

pause
