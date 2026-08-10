@echo off
REM ===========================================================================
REM  Vays Newsletter Platform - start the app
REM
REM  Double-click this file, or run it from a terminal. It checks the things
REM  that actually go wrong on a fresh machine and says what to do about each,
REM  rather than failing with a Python traceback the marketing team cannot read.
REM ===========================================================================
setlocal
cd /d "%~dp0"

echo.
echo   Vays Newsletter Platform
echo   ------------------------
echo.

REM --- 1. virtual environment ------------------------------------------------
if not exist ".venv\Scripts\python.exe" (
    echo   [X] No virtual environment found.
    echo.
    echo       Run this once to create it:
    echo           py -3.12 -m venv .venv
    echo           .venv\Scripts\python.exe -m pip install -r requirements.lock.txt
    echo.
    echo       See docs\SETUP_GUIDE.md for the full first-run steps.
    echo.
    pause
    exit /b 1
)

REM --- 2. configuration ------------------------------------------------------
if not exist ".env" (
    echo   [X] No .env file found.
    echo.
    if exist ".env.example" (
        echo       Copy the example and fill in your API key:
        echo           copy .env.example .env
        echo.
    )
    echo       See docs\SETUP_GUIDE.md section 3.
    echo.
    pause
    exit /b 1
)

REM --- 3. database -----------------------------------------------------------
REM  Alembic is idempotent: on an existing database this is a no-op, so it is
REM  safe to run on every start. It is what makes an upgrade a pull-and-run.
echo   Applying database migrations...
.venv\Scripts\python.exe -m alembic upgrade head
if errorlevel 1 (
    echo.
    echo   [X] Migrations failed. The app has not been started.
    echo       See docs\RUNBOOK.md - "Database problems".
    echo.
    pause
    exit /b 1
)

REM --- 4. first-run account --------------------------------------------------
REM  A login screen with no accounts is a dead end, so check before serving.
.venv\Scripts\python.exe -c "import sys; sys.path.insert(0,'.'); from modules.repository.database import init_database; from services.auth_service import AuthService; init_database(); sys.exit(0 if AuthService().has_any_users() else 1)" >nul 2>&1
if errorlevel 1 (
    echo.
    echo   [!] No user accounts exist yet - you would not be able to sign in.
    echo.
    echo       Create the first one:
    echo           .venv\Scripts\python.exe scripts\create_user.py
    echo.
    pause
    exit /b 1
)

REM --- 5. go -----------------------------------------------------------------
echo.
echo   Starting. The app opens at http://localhost:8501
echo   Press Ctrl+C in this window to stop it.
echo.
.venv\Scripts\python.exe -m streamlit run app.py

endlocal
