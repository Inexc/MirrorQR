@echo off
chcp 65001 >nul
title TranSlipt - Экран по QR (Веб-панель)

echo ========================================================
echo   TranSlipt — Экран по QR (Режим веб-панели)
echo ========================================================
echo.

if exist ".venv313\Scripts\python.exe" (
    set "PY=.venv313\Scripts\python.exe"
) else if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    set "PY=python"
)

echo Запуск сервера...
start "" cmd /c "timeout /t 2 /nobreak >nul & start http://127.0.0.1:8766/desktop"
%PY% main.py --web

if %errorlevel% neq 0 (
    echo.
    echo [Ошибка] Не удалось запустить сервер. Проверьте установку зависимостей:
    echo pip install -r requirements.txt
    pause
)
