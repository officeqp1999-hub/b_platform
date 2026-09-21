@echo off
chcp 65001 >nul
setlocal EnableExtensions
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
title Центр управления - остановка
echo ============================================================
echo    Центр управления - остановка
echo ============================================================
echo.
if not exist "venv\Scripts\python.exe" goto not_installed
venv\Scripts\python.exe run.py stop
if errorlevel 1 goto failed
echo.
pause
exit /b 0

:not_installed
echo Программа ещё не установлена, останавливать нечего.
echo.
pause
exit /b 0

:failed
echo.
echo Не получилось остановить панель. Прочитайте сообщение выше - в нём написано, что делать.
echo.
pause
exit /b 1
