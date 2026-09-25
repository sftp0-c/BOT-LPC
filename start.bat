@echo off
setlocal
cd /d "%~dp0"
title BOT-LPC

echo ============================================
echo   BOT-LPC - запуск бота (Windows)
echo ============================================
echo.

rem --- ищем Python 3.11+ -------------------------------------------
set "PY="
py -3.12 --version >nul 2>nul && set "PY=py -3.12"
if not defined PY py -3.11 --version >nul 2>nul && set "PY=py -3.11"
if not defined PY py -3 --version >nul 2>nul && set "PY=py -3"
if not defined PY python --version >nul 2>nul && set "PY=python"
if not defined PY (
    echo [ОШИБКА] Python не найден.
    echo Установите Python 3.11 или новее: https://www.python.org/downloads/
    echo ВАЖНО: при установке отметьте галочку "Add python.exe to PATH".
    echo.
    pause
    exit /b 1
)

%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if errorlevel 1 (
    echo [ОШИБКА] Найден слишком старый Python. Нужен 3.11 или новее.
    echo Обновите его: https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)
echo Python: OK

rem --- первое открытие .env для заполнения --------------------------
if not exist ".env" copy /y ".env.example" ".env" >nul
findstr /r "PASTE_YOUR MAX_USER_ID" ".env" >nul 2>nul
if not errorlevel 1 (
    echo.
    echo Открываю файл .env - заполните MAX_BOT_TOKEN и SYSADMIN_IDS,
    echo сохраните (Ctrl+O, Enter) и выйдите (Ctrl+X) в открывшемся Блокноте.
    echo.
    notepad ".env"
)

rem --- виртуальное окружение + зависимости ---------------------------
if not exist ".venv\Scripts\python.exe" (
    echo.
    echo Создаю виртуальное окружение...
    %PY% -m venv .venv
)

set "VPY=.venv\Scripts\python.exe"

"%VPY%" -c "import fastapi, httpx, aiosqlite, uvicorn" >nul 2>nul
if errorlevel 1 (
    echo.
    echo Устанавливаю зависимости ^(в первый раз это займет пару минут^)...
    "%VPY%" -m pip install --upgrade pip >nul
    "%VPY%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo [ОШИБКА] Не удалось установить зависимости.
        echo Проверьте интернет-соединение и запустите start.bat снова.
        pause
        exit /b 1
    )
)

rem --- запуск --------------------------------------------------------
echo.
echo Запускаю бота...
echo   - Настройки: файл .env ^(токен бота и ID сис-админа^)
echo   - База данных: папка data\ ^(сохраняется автоматически^)
echo   - Остановка: Ctrl+C или просто закрыть это окно
echo.
"%VPY%" -m uvicorn bot:app --host 0.0.0.0 --port 8080
echo.
pause
