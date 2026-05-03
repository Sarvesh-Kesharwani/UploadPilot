@echo off
REM One-shot setup: creates venv, installs deps, installs Playwright browsers.

setlocal
set "PROJECT_DIR=%~dp0.."
for %%I in ("%PROJECT_DIR%") do set "PROJECT_DIR=%%~fI"
cd /d "%PROJECT_DIR%"
if not exist ".tmp" mkdir ".tmp"
set "TMP=%PROJECT_DIR%\.tmp"
set "TEMP=%PROJECT_DIR%\.tmp"

set "BASE_PY_EXE="
set "BASE_PY_ARG="
set "BASE_PY_LABEL="

for /f "delims=" %%I in ('dir /b /s "%APPDATA%\uv\python\python.exe" 2^>nul') do (
  set "BASE_PY_EXE=%%~fI"
  set "BASE_PY_LABEL=%%~fI"
  goto :found_python
)

where py >nul 2>&1
if not errorlevel 1 (
  py -3.13 -c "import sys" >nul 2>&1
  if not errorlevel 1 (
    set "BASE_PY_EXE=py"
    set "BASE_PY_ARG=-3.13"
    set "BASE_PY_LABEL=py -3.13"
    goto :found_python
  )
  py -3 -c "import sys" >nul 2>&1
  if not errorlevel 1 (
    set "BASE_PY_EXE=py"
    set "BASE_PY_ARG=-3"
    set "BASE_PY_LABEL=py -3"
    goto :found_python
  )
)

where python >nul 2>&1
if not errorlevel 1 (
  python -c "import sys; import pathlib; p=pathlib.Path(sys.executable); raise SystemExit(1 if 'WindowsApps' in str(p) else 0)" >nul 2>&1
  if not errorlevel 1 (
    set "BASE_PY_EXE=python"
    set "BASE_PY_LABEL=python"
    goto :found_python
  )
)

echo Could not find a usable Python interpreter.
echo Install Python 3.11+ or the uv-managed Python toolchain, then rerun this script.
exit /b 1

:found_python
echo Using Python launcher: %BASE_PY_LABEL%

set "REBUILD_VENV=0"
if exist ".venv\pyvenv.cfg" (
  findstr /i "WindowsApps" ".venv\pyvenv.cfg" >nul 2>&1 && set "REBUILD_VENV=1"
)

if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "import sys" >nul 2>&1 || set "REBUILD_VENV=1"
  ".venv\Scripts\python.exe" -m pip --version >nul 2>&1 || set "REBUILD_VENV=1"
) else (
  set "REBUILD_VENV=1"
)

if "%REBUILD_VENV%"=="1" (
  if exist ".venv" (
    echo Rebuilding broken virtualenv at .venv ...
    rmdir /s /q ".venv"
  ) else (
    echo Creating virtualenv at .venv ...
  )
  call "%BASE_PY_EXE%" %BASE_PY_ARG% -m venv .venv
)

if not exist ".venv\Scripts\python.exe" (
  echo Virtualenv creation failed.
  exit /b 1
)

".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
".venv\Scripts\python.exe" -m playwright install chromium

if not exist "config.yaml" (
  echo Copying config.example.yaml -> config.yaml
  copy /y config.example.yaml config.yaml >nul
)

echo.
echo Setup complete.
echo Next: run scripts\install_autostart.bat to enable auto-launch on login.
pause
