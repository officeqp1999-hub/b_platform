@echo off
chcp 65001 >nul
setlocal EnableExtensions
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
title Центр управления - запуск
echo ============================================================
echo    Центр управления - запуск
echo ============================================================
echo.
if not exist "venv\Scripts\python.exe" goto not_installed
venv\Scripts\python.exe run.py start
if errorlevel 1 goto failed
echo.
echo Готово. Это окно можно закрыть - панель продолжит работать в фоне.
echo Остановить панель: остановить.bat
echo.
pause
exit /b 0

:not_installed
echo ОШИБКА: программа ещё не установлена.
echo.
echo ЧТО ДЕЛАТЬ: сначала запустите установить.bat и дождитесь надписи "Установка завершена".
echo.
pause
exit /b 1

:failed
echo.
echo Панель не запустилась. Прочитайте сообщение выше - в нём написано, что делать.
echo Подробности записаны в файл logs\server.log
echo.
pause
exit /b 1
