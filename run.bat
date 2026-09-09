@echo off
setlocal enabledelayedexpansion

rem run.bat — run the scraper or matcher, in dry-run or prod mode. Windows
rem equivalent of run.sh.
rem
rem Usage:
rem   run.bat scraper dry     scrapes and prints, no DB writes
rem   run.bat scraper prod    real run, writes to `jobs`
rem   run.bat matcher dry     scores via real API calls, no DB writes
rem   run.bat matcher prod    real run, writes to `matches`

set "SCRIPT_DIR=%~dp0"
set "PYTHON=%SCRIPT_DIR%.venv\Scripts\python.exe"

set "TARGET=%~1"
set "MODE=%~2"

if "%TARGET%"=="" goto usage
if "%MODE%"=="" goto usage

if /I not "%TARGET%"=="scraper" if /I not "%TARGET%"=="matcher" goto usage
if /I not "%MODE%"=="dry" if /I not "%MODE%"=="prod" goto usage

if /I "%TARGET%"=="scraper" (
    set "SCRIPT=%SCRIPT_DIR%scrapper.py"
    set "ENV_VAR=SCRAPER_DRY_RUN"
) else (
    set "SCRIPT=%SCRIPT_DIR%matcher.py"
    set "ENV_VAR=MATCHER_DRY_RUN"
)

if /I "%MODE%"=="dry" (
    echo === Running %TARGET% in DRY-RUN mode ^(no DB writes^) ===
    set "!ENV_VAR!=1"
) else (
    echo === Running %TARGET% in PROD mode ^(writes to DB^) ===
    set "!ENV_VAR!="
)

"%PYTHON%" -u "%SCRIPT%"
exit /b %ERRORLEVEL%

:usage
echo Usage: run.bat ^<scraper^|matcher^> ^<dry^|prod^>
exit /b 1
