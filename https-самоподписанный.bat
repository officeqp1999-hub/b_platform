@echo off
chcp 65001 >nul
setlocal EnableExtensions
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
title Центр управления - https для внутренней сети
echo ============================================================
echo    Включение https на самоподписанном сертификате
echo ============================================================
echo.
echo Это быстрый способ зашифровать соединение внутри сети, чтобы пароли не шли открытым текстом.
echo Для доступа из интернета и для входа через Google нужен настоящий сертификат на домен
echo (см. ИНСТРУКЦИЮ, раздел "Доступ снаружи").
echo.
if not exist "venv\Scripts\python.exe" goto not_installed
venv\Scripts\python.exe run.py selfsigned
if errorlevel 1 goto failed
echo.
echo Теперь перезапустите панель: остановить.bat, потом запустить.bat.
echo.
pause
exit /b 0

:not_installed
echo ОШИБКА: программа ещё не установлена. ЧТО ДЕЛАТЬ: запустите установить.bat.
echo.
pause
exit /b 1

:failed
echo.
echo Сертификат не создан. Прочитайте сообщение выше.
echo.
pause
exit /b 1
