@echo off
chcp 65001 >nul
setlocal EnableExtensions
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
title Сброс пароля администратора
echo ============================================================
echo    Сброс пароля администратора
echo ============================================================
echo.
if not exist "venv\Scripts\python.exe" goto not_installed
echo Пароль станет:  admin   (при входе панель сразу попросит новый).
echo Подключения и настройки не затрагиваются.
echo.
set /p CONFIRM=Продолжить? Введите цифру 1 и нажмите Enter: 
if not "%CONFIRM%"=="1" goto cancelled
echo.
venv\Scripts\python.exe run.py resetpw %1
if errorlevel 1 goto failed
echo.
pause
exit /b 0

:cancelled
echo Отменено, ничего не изменено.
echo.
pause
exit /b 0

:not_installed
echo Программа ещё не установлена. Запустите установить.bat.
echo.
pause
exit /b 1

:failed
echo.
echo Пароль не сброшен. Прочитайте сообщение выше - в нём написано, что делать.
echo Если администраторов несколько, запустите так:  сброс-пароля.bat ЛОГИН
echo.
pause
exit /b 1
