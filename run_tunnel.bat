@echo off
REM ===========================================================================
REM  Vays Newsletter Platform - public tunnel (ngrok)
REM
REM  Gives the dashboard a public HTTPS address so an approval link works from
REM  a phone or a manager's laptop, not only from this PC.
REM
REM  Reads APP_PORT and NGROK_DOMAIN from .env, so there is one place to change
REM  them. Leave this window open alongside the dashboard.
REM ===========================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo   Vays Newsletter - public tunnel
echo   -------------------------------
echo.

where ngrok >nul 2>&1
if errorlevel 1 (
    echo   [X] ngrok is not installed, or not on this shell's PATH.
    echo       Install:  winget install ngrok.ngrok
    echo       Then open a NEW terminal ^(PATH changes need a fresh shell^).
    echo.
    echo       See docs\PUBLIC_ACCESS.md.
    echo.
    pause
    exit /b 1
)

REM --- read APP_PORT and NGROK_DOMAIN out of .env ---------------------------
set "APP_PORT=8501"
set "NGROK_DOMAIN="
if exist ".env" (
    for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
        if /i "%%A"=="APP_PORT"     set "APP_PORT=%%B"
        if /i "%%A"=="NGROK_DOMAIN" set "NGROK_DOMAIN=%%B"
    )
)

REM Strip stray whitespace a hand-edited .env can leave behind.
for /f "tokens=* delims= " %%A in ("!APP_PORT!") do set "APP_PORT=%%A"
for /f "tokens=* delims= " %%A in ("!NGROK_DOMAIN!") do set "NGROK_DOMAIN=%%A"

echo   Forwarding to  : http://localhost:!APP_PORT!
echo.

if not "!NGROK_DOMAIN!"=="" (
    echo   Reserved domain: !NGROK_DOMAIN!
    echo   This address is stable, so emailed approval links keep working.
    echo.
    echo   Set in .env:  AGENT_APP_BASE_URL=https://!NGROK_DOMAIN!
    echo.
    ngrok http !APP_PORT! --domain=!NGROK_DOMAIN!
) else (
    echo   Temporary address ^(changes every restart^).
    echo   AGENT_APP_BASE_URL=auto picks it up for NEW emails, but links already
    echo   sent will stop working when this restarts.
    echo.
    echo   Claim a free reserved domain to avoid that:
    echo     https://dashboard.ngrok.com/domains
    echo   then put it in .env as NGROK_DOMAIN and restart this.
    echo.
    ngrok http !APP_PORT!
)

endlocal
