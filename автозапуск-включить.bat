@echo off
chcp 65001 >nul
setlocal EnableExtensions
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
title Центр управления - включение автозапуска
echo ============================================================
echo    Автозапуск: панель будет включаться вместе с Windows
echo ============================================================
echo.
net session >nul 2>nul
if errorlevel 1 goto need_admin
if not exist "venv\Scripts\pythonw.exe" goto not_installed
schtasks /Create /TN "Platforma-Centr-Upravleniya" /TR "\"%~dp0venv\Scripts\pythonw.exe\" \"%~dp0run.py\" serve" /SC ONSTART /DELAY 0000:30 /RU SYSTEM /RL HIGHEST /F
if errorlevel 1 goto failed
echo.
echo Готово. При каждом включении или перезагрузке сервера панель запустится сама
echo примерно через полминуты после старта Windows.
echo Сейчас запущенную панель трогать не нужно - она продолжит работать.
echo Отключить автозапуск: автозапуск-выключить.bat
echo.
pause
exit /b 0

:need_admin
echo ОШИБКА: этому файлу нужны права администратора.
echo.
echo ЧТО ДЕЛАТЬ: закройте это окно, нажмите на файл автозапуск-включить.bat ПРАВОЙ кнопкой мыши
echo и выберите "Запуск от имени администратора".
echo.
pause
exit /b 1

:not_installed
echo ОШИБКА: программа ещё не установлена.
echo ЧТО ДЕЛАТЬ: сначала запустите установить.bat.
echo.
pause
exit /b 1

:failed
echo.
echo ОШИБКА: Windows не смогла создать задачу автозапуска (см. сообщение выше).
echo ЧТО ДЕЛАТЬ: убедитесь, что вы запускаете файл от имени администратора и что служба
echo "Планировщик заданий" в Windows включена. Если не помогло - отправьте фото экрана разработчику.
echo.
pause
exit /b 1
