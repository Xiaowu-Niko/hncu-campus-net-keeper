@echo off
REM ==========================================================================
REM  Campus Network Keeper - thin launcher shim
REM --------------------------------------------------------------------------
REM  All real logic lives in keeper.ps1 (PowerShell), because cmd.exe quoting
REM  and errorlevel handling are too fragile for process supervision.
REM
REM  This file MUST stay ASCII-only. cmd.exe parses .bat in the OEM codepage
REM  (GBK here); CJK byte sequences can end in 0x5C (backslash), which escapes
REM  the newline and splices lines together, corrupting the script.
REM
REM  Every path is derived from %~dp0, so the folder can live anywhere.
REM --------------------------------------------------------------------------
REM    start_keeper.bat            start hidden + auto-restart on crash
REM    start_keeper.bat /visible   foreground run (debug, Ctrl+C stops)
REM    start_keeper.bat /stop      stop keeper + supervisor
REM    start_keeper.bat /restart   stop then start
REM    start_keeper.bat /status    show processes, proxy state, logs
REM    start_keeper.bat /check     validate config + probe the line
REM    start_keeper.bat /help      show help
REM ==========================================================================

setlocal EnableExtensions
set "SCRIPT_DIR=%~dp0"
set "PS1=%SCRIPT_DIR%keeper.ps1"

if not exist "%PS1%" (
    echo [ERROR] keeper.ps1 not found next to this launcher:
    echo         %PS1%
    pause
    exit /b 1
)

set "ACTION=start"
if /i "%~1"=="/stop"    set "ACTION=stop"
if /i "%~1"=="/restart" set "ACTION=restart"
if /i "%~1"=="/status"  set "ACTION=status"
if /i "%~1"=="/check"   set "ACTION=check"
if /i "%~1"=="/visible" set "ACTION=visible"
if /i "%~1"=="/help"    set "ACTION=help"
if /i "%~1"=="-h"       set "ACTION=help"
if /i "%~1"=="--help"   set "ACTION=help"

if /i "%ACTION%"=="visible" goto RUN_VISIBLE
if /i "%ACTION%"=="check"   goto RUN_CHECK

REM Hidden, non-interactive run for start/stop/restart/status.
powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%PS1%" %ACTION%
set "RC=%ERRORLEVEL%"
endlocal & exit /b %RC%

:RUN_VISIBLE
echo [INFO] Foreground mode. Press Ctrl+C to stop.
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" visible
set "RC=%ERRORLEVEL%"
echo.
pause
endlocal & exit /b %RC%

:RUN_CHECK
echo [INFO] Checking config and line status...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" check
set "RC=%ERRORLEVEL%"
echo.
pause
endlocal & exit /b %RC%
