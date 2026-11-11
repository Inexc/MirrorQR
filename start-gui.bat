@echo off
chcp 65001 >nul
title MirrorQR - Экран по QR (Окно)

echo ========================================================
echo   MirrorQR — Экран по QR (Графический интерфейс)
echo ========================================================
echo.

if exist ".venv313\Scripts\python.exe" (
    set "PY=.venv313\Scripts\python.exe"
) else if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    set "PY=python"
)

echo Запуск приложения...
%PY% main.py

if %errorlevel% neq 0 (
    echo.
    echo [Ошибка] Не удалось запустить приложение. Проверьте установку зависимостей:
    echo pip install -r requirements.txt
    pause
)
