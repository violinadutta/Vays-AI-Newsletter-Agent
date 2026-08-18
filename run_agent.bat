@echo off
REM ===========================================================================
REM  Vays Newsletter Platform - automation worker
REM
REM  Runs the agent: discovers new blog posts, drafts newsletters, asks for
REM  approval, and sends approved campaigns at the configured time.
REM
REM  This is SEPARATE from run.bat. The dashboard only runs while a browser is
REM  open; unattended automation needs its own process, which is this one.
REM  Leave the window open.
REM ===========================================================================
setlocal
cd /d "%~dp0"

echo.
echo   Vays Newsletter Agent
echo   ---------------------
echo.

if not exist ".venv\Scripts\python.exe" (
    echo   [X] No virtual environment found.
    echo       See docs\SETUP_GUIDE.md, then run this again.
    echo.
    pause
    exit /b 1
)

if not exist ".env" (
    echo   [X] No .env file found. Copy .env.example to .env first.
    echo.
    pause
    exit /b 1
)

REM Migrations are idempotent, and the agent's tables must exist before it runs.
.venv\Scripts\python.exe -m alembic upgrade head >nul 2>&1
if errorlevel 1 (
    echo   [X] Database migrations failed. Start the dashboard once with run.bat
    echo       to see the error, then try again.
    echo.
    pause
    exit /b 1
)

.venv\Scripts\python.exe agent_worker.py
if errorlevel 1 (
    echo.
    echo   The agent did not start. The reason is printed above.
    echo.
    pause
)

endlocal
