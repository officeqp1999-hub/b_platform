@echo off
chcp 65001 >nul
setlocal EnableExtensions
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
title Демо-режим - Центр управления
echo ============================================================
echo    Демо-режим: панель с поддельными сервисами
echo ============================================================
echo.
if not exist "venv\Scripts\python.exe" goto not_installed
echo Настоящие данные (папка data) не затрагиваются.
echo Через несколько секунд в браузере откроется панель.
echo Чтобы закончить - закройте это окно.
echo.
venv\Scripts\python.exe tests\demo.py
if errorlevel 1 goto failed
exit /b 0

:not_installed
echo Программа ещё не установлена.
echo ЧТО ДЕЛАТЬ: сначала запустите установить.bat, потом снова демо.bat.
echo.
pause
exit /b 1

:failed
echo.
echo Демо не запустилось. Прочитайте сообщение выше - в нём написано, что делать.
echo.
pause
exit /b 1
