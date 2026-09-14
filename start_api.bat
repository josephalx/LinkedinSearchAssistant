@echo off
setlocal

rem start_api.bat — start the dashboard API server. Windows equivalent of
rem start_api.sh.
rem
rem Usage:
rem   start_api.bat          defaults to dry-run mode for dashboard-triggered runs
rem   start_api.bat dry      same, explicit
rem   start_api.bat prod     dashboard-triggered scraper/matcher runs will write to the DB

set "SCRIPT_DIR=%~dp0"
set "PYTHON=%SCRIPT_DIR%.venv\Scripts\python.exe"
set "MODE=%~1"
if "%MODE%"=="" set "MODE=dry"

if /I not "%MODE%"=="dry" if /I not "%MODE%"=="prod" (
    echo Usage: start_api.bat [dry^|prod]
    exit /b 1
)

if /I "%MODE%"=="dry" (
    echo === Starting API — dashboard-triggered runs will be DRY-RUN ^(no DB writes^) ===
    set "SCRAPER_DRY_RUN=1"
    set "MATCHER_DRY_RUN=1"
) else (
    echo === Starting API — dashboard-triggered runs will be PROD ^(writes to DB^) ===
    set "SCRAPER_DRY_RUN=0"
    set "MATCHER_DRY_RUN=0"
)

rem Which scraper the dashboard launches; set SCRAPER_ENGINE=selenium for the original.
if not defined SCRAPER_ENGINE set "SCRAPER_ENGINE=playwright"

"%PYTHON%" -u "%SCRIPT_DIR%dashboard\api.py"
