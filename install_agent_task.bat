@echo off
REM ===========================================================================
REM  Register the automation agent with Windows Task Scheduler.
REM
REM  Run this ONCE, as Administrator. After it, the agent runs every hour with
REM  no window and starts itself after a reboot — you no longer have to remember
REM  to open run_agent.bat.
REM
REM  Why --once rather than the long-running worker: a scheduled task wants a
REM  process that does its work and exits. A long-lived one that dies silently
REM  looks identical to one that is working, which is this system's worst
REM  failure mode.
REM
REM  Hourly, not 6-hourly, because the same pass also checks whether an approved
REM  campaign is due to send. Discovery does almost nothing when there is nothing
REM  new, so the extra runs are close to free.
REM ===========================================================================
setlocal
cd /d "%~dp0"

schtasks /create /tn "Vays Newsletter Agent" ^
  /tr "\"%~dp0.venv\Scripts\pythonw.exe\" \"%~dp0agent_worker.py\" --once" ^
  /sc hourly /mo 1 /f

if errorlevel 1 (
    echo.
    echo   [X] Could not create the task. Right-click this file and choose
    echo       "Run as administrator".
    echo.
) else (
    echo.
    echo   Registered. The agent now runs every hour in the background.
    echo.
    echo   Check it     : schtasks /query /tn "Vays Newsletter Agent"
    echo   Run it now   : schtasks /run   /tn "Vays Newsletter Agent"
    echo   Remove it    : schtasks /delete /tn "Vays Newsletter Agent" /f
    echo.
    echo   You can close run_agent.bat once this is registered.
    echo.
)
pause
endlocal
