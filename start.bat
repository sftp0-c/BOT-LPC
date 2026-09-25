@echo off
setlocal enableextensions enabledelayedexpansion
title BOT-LPC
cd /d "%~dp0"

echo ============================================
echo   BOT-LPC - start (Windows)
echo ============================================
echo.

rem ---- sanity check: are we inside the project? ----
if not exist "requirements.txt" (
    echo [ERROR] requirements.txt not found next to start.bat
    echo Put this file INTO the BOT-LPC folder, where bot.py is.
    goto fail
)

rem ---- find a working Python 3.11-3.13 ----
set "PY="
for %%V in (3.12 3.13 3.11) do (
    if not defined PY (
        py -%%V --version >nul 2>nul && py -%%V -c "import sys; sys.exit(0 if sys.version_info>=(3,11) else 1)" >nul 2>nul && set "PY=py -%%V"
    )
)
if not defined PY (
    python --version >nul 2>nul && python -c "import sys; sys.exit(0 if sys.version_info>=(3,11) and sys.version_info<(3,14) else 1)" >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo [ERROR] No usable Python 3.11-3.13 found.
    echo Install Python 3.12 from https://www.python.org/downloads/
    echo and CHECK "Add python.exe to PATH" during install.
    goto fail
)
for /f "delims=" %%I in ('%PY% --version') do echo Found: %%I

rem ---- rebuild venv if it points to a broken/deleted Python ----
set "VPY=.venv\Scripts\python.exe"
if exist ".venv\Scripts\python.exe" (
    "%VPY%" --version >nul 2>nul
    if errorlevel 1 (
        echo.
        echo Old .venv is broken ^(points to a removed Python^). Recreating...
        rmdir /s /q ".venv"
    )
)
if not exist ".venv\Scripts\python.exe" (
    echo.
    echo Creating virtual environment...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Could not create .venv
        goto fail
    )
)

rem ---- verify interpreter really works ----
"%VPY%" --version >nul 2>nul
if errorlevel 1 (
    echo.
    echo [ERROR] The venv Python is broken:
    for /f "delims=" %%I in ('"%VPY%" --version 2^>^&1') do echo   %%I
    echo Fix: delete the .venv folder manually and run start.bat again.
    goto fail
)

rem ---- first run: create .env with REAL defaults ----
if not exist ".env" (
    >".env" echo MAX_BOT_TOKEN=PASTE_YOUR_TOKEN_HERE
    >>".env" echo MAX_API_URL=https://platform-api2.max.ru
    >>".env" echo MAX_WEBHOOK_URL=
    >>".env" echo MAX_WEBHOOK_SECRET=change_me_to_random_secret
    >>".env" echo SYSADMIN_IDS=YOUR_MAX_USER_ID
    >>".env" echo DATABASE_PATH=data/database.db
    >>".env" echo LOG_LEVEL=INFO
    >>".env" echo LOG_FILE=logs/bot.log
    >>".env" echo WEB_PANEL_PASSWORD=
    >>".env" echo WEB_PANEL_HOURS=12
    echo.
    echo First run: Notepad will open .env.
    echo   1. MAX_BOT_TOKEN      = token from dev.max.ru
    echo   2. SYSADMIN_IDS       = your MAX id ^(command /id in the bot chat^)
    echo   3. WEB_PANEL_PASSWORD = any password for the web panel ^(or leave empty^)
    echo Save with Ctrl+S and CLOSE Notepad. The bot continues automatically.
    echo.
    notepad ".env"
    if exist ".env.txt" move /y ".env.txt" ".env" >nul
)

rem Notepad sometimes saves as .env.txt instead of .env - merge it in
if exist ".env.txt" move /y ".env.txt" ".env" >nul

rem ---- validate .env contents, show EXACTLY what is wrong ----
echo.
echo Checking .env ...
set "BAD="
findstr /r "^MAX_BOT_TOKEN=[A-Za-z0-9_-][A-Za-z0-9_-]*" ".env" >nul 2>nul || set "BAD=%BAD% MAX_BOT_TOKEN"
findstr /r "^SYSADMIN_IDS=[0-9][0-9, ]*" ".env" >nul 2>nul || set "BAD=%BAD% SYSADMIN_IDS"
if defined BAD (
    echo.
    echo ============================================================
    echo  [PROBLEM] These settings in .env are NOT filled in properly:
    echo %BAD%
    echo ------------------------------------------------------------
    echo  MAX_BOT_TOKEN must contain ONLY the token on its own line:
    echo      MAX_BOT_TOKEN=PASTE_YOUR_TOKEN_HERE
    echo  ^(! Do NOT copy the comment line that starts with #^)
    echo  SYSADMIN_IDS must be a NUMBER, e.g.:
    echo      SYSADMIN_IDS=2857023211
    echo  ^(! Replace YOUR_MAX_USER_ID with digits from the /id command^)
    echo ============================================================
    echo.
    echo Opening .env in Notepad now. Fix it, save ^(Ctrl+S^), close Notepad.
    notepad ".env"
    if exist ".env.txt" move /y ".env.txt" ".env" >nul
    echo.
    echo Then run start.bat again.
    pause
    exit /b 0
)
echo .env looks OK.

rem ---- dependencies (verify they really import) ----
set "DEPS=fastapi, uvicorn, httpx, aiosqlite, dotenv, multipart"
"%VPY%" -c "import %DEPS%" >nul 2>nul
if errorlevel 1 (
    echo.
    echo Installing dependencies ^(first run takes a couple of minutes^)... 
    "%VPY%" -m pip install --upgrade pip >nul 2>nul
    "%VPY%" -m pip install -r requirements.txt
    "%VPY%" -c "import %DEPS%" >nul 2>nul
    if errorlevel 1 (
        echo.
        echo [ERROR] Dependencies installed but cannot be imported.
        echo Try: delete the .venv folder and run start.bat again.
        goto fail
    )
)


if not exist "data" mkdir "data"

powershell -NoProfile -Command "$listener = Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue; if (-not $listener) { exit 1 }; try { $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8080/health' -TimeoutSec 2; if ($health.platform -eq 'MAX') { exit 0 } } catch {}; exit 2"
set "PORT_STATUS=%ERRORLEVEL%"
if "%PORT_STATUS%"=="0" (
    echo.
    echo BOT-LPC is already running: http://localhost:8080/health
    exit /b 0
)
if "%PORT_STATUS%"=="2" (
    echo.
    echo [ERROR] Port 8080 is occupied by another application.
    echo Stop that application or free port 8080, then run start.bat again.
    goto fail
)

set "CUSTOM_CA="
for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
    if /I "%%A"=="MAX_CA_BUNDLE" set "CUSTOM_CA=%%B"
)
if defined CUSTOM_CA (
    if not exist "%CUSTOM_CA%" (
        echo.
        echo [ERROR] MAX_CA_BUNDLE does not exist: %CUSTOM_CA%
        goto fail
    )
) else (
    if not exist ".certs" mkdir ".certs"
    if not exist ".certs\max-ca.pem" (
        echo.
        echo Downloading Russian trusted CA certificates...
        curl.exe -fsSL "https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt" -o ".certs\root.crt"
        if errorlevel 1 (
            echo [ERROR] Could not download the root CA certificate.
            goto fail
        )
        curl.exe -fsSL "https://gu-st.ru/content/lending/russian_trusted_sub_ca_pem.crt" -o ".certs\sub.crt"
        if errorlevel 1 (
            echo [ERROR] Could not download the intermediate CA certificate.
            goto fail
        )
        copy /b ".certs\root.crt"+".certs\sub.crt" ".certs\max-ca.pem" >nul
        findstr /c:"BEGIN CERTIFICATE" ".certs\max-ca.pem" >nul || (
            echo [ERROR] Downloaded CA bundle is invalid.
            goto fail
        )
    )
)

echo Checking MAX API connection...
"%VPY%" -c "import asyncio; from max_api import MaxAPI; asyncio.run(MaxAPI().me())"
if errorlevel 1 (
    echo.
    echo [ERROR] MAX API is unavailable. Check the token, network, and CA certificates.
    goto fail
)

rem ---- run ----
echo.
echo Starting bot on http://localhost:8080  ^(health check: /health^)
echo Web admin panel: http://localhost:8080/panel  ^(MAX ID + WEB_PANEL_PASSWORD^)
echo Stop: press Ctrl+C or close this window.
echo.
"%VPY%" -m uvicorn bot:app --host 0.0.0.0 --port 8080
echo.
echo ===== Bot process ended. Read the text above for details. =====
pause
exit /b 0

:fail
echo.
echo ===== Startup failed. See the [ERROR] message above. =====
pause
exit /b 1