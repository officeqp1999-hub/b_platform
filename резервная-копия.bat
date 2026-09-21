@echo off
chcp 65001 >nul
setlocal EnableExtensions
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
title Центр управления - резервная копия
echo ============================================================
echo    Центр управления - резервная копия
echo ============================================================
echo.
if not exist "venv\Scripts\python.exe" goto not_installed
venv\Scripts\python.exe run.py backup
set "RC=%errorlevel%"
if not "%RC%"=="0" goto failed
if /i "%~1"=="auto" exit /b 0
echo.
echo Резервная копия сделана. Скопируйте файл из указанной выше папки на другой диск или компьютер.
echo.
pause
exit /b 0

:not_installed
echo ОШИБКА: программа ещё не установлена, копировать нечего.
echo ЧТО ДЕЛАТЬ: запустите установить.bat.
echo.
if /i "%~1"=="auto" exit /b 1
pause
exit /b 1

:failed
echo.
echo Резервная копия НЕ сделана. Прочитайте сообщение выше - в нём написано, что делать.
echo.
if /i "%~1"=="auto" exit /b 1
pause
exit /b 1
