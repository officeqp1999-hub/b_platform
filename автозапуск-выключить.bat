@echo off
chcp 65001 >nul
setlocal EnableExtensions
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
title Центр управления - отключение автозапуска
echo ============================================================
echo    Автозапуск: отключение
echo ============================================================
echo.
net session >nul 2>nul
if errorlevel 1 goto need_admin
schtasks /Query /TN "Platforma-Centr-Upravleniya" >nul 2>nul
if errorlevel 1 goto not_set
schtasks /Delete /TN "Platforma-Centr-Upravleniya" /F
if errorlevel 1 goto failed
echo.
echo Готово. Автозапуск отключён. Уже работающая панель продолжает работать -
echo чтобы её остановить, запустите остановить.bat.
echo.
pause
exit /b 0

:need_admin
echo ОШИБКА: этому файлу нужны права администратора.
echo.
echo ЧТО ДЕЛАТЬ: закройте это окно, нажмите на файл автозапуск-выключить.bat ПРАВОЙ кнопкой мыши
echo и выберите "Запуск от имени администратора".
echo.
pause
exit /b 1

:not_set
echo Автозапуск и так не был включён - ничего делать не нужно.
echo.
pause
exit /b 0

:failed
echo.
echo ОШИБКА: не удалось удалить задачу автозапуска (см. сообщение выше).
echo ЧТО ДЕЛАТЬ: убедитесь, что вы запускаете файл от имени администратора. Если не помогло - отправьте фото экрана разработчику.
echo.
pause
exit /b 1
