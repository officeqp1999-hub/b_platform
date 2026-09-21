@echo off
chcp 65001 >nul
setlocal EnableExtensions
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
title Центр управления - установка
echo ============================================================
echo    Центр управления - установка
echo ============================================================
echo.
echo Ничего вводить и настраивать не нужно. Просто дождитесь надписи "Установка завершена".
echo.

echo [1 из 5] Ищу Python 3.11 или новее...
set "PY="
py -3.11 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PY=py -3.11"
if defined PY goto py_ok
py -3 -c "import sys; sys.exit(0 if sys.version_info>=(3,11) else 1)" >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if defined PY goto py_ok
python -c "import sys; sys.exit(0 if sys.version_info>=(3,11) else 1)" >nul 2>nul
if not errorlevel 1 set "PY=python"
if defined PY goto py_ok
goto no_python

:py_ok
echo     Найден: %PY%
echo.
echo [2 из 5] Создаю рабочую среду (папка venv)...
if exist "venv\Scripts\python.exe" goto venv_ok
%PY% -m venv venv
if errorlevel 1 goto venv_fail
:venv_ok
echo     Готово.
echo.
echo [3 из 5] Устанавливаю библиотеки. Нужен интернет, это занимает 1-3 минуты...
if exist "wheels\" goto pip_offline
venv\Scripts\python.exe -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto pip_fail
goto pip_ok
:pip_offline
echo     Найдена папка wheels - ставлю без интернета.
venv\Scripts\python.exe -m pip install --disable-pip-version-check --no-index --find-links wheels -r requirements.txt
if errorlevel 1 goto pip_fail
:pip_ok
venv\Scripts\python.exe run.py check
if errorlevel 1 goto pip_fail
echo.
echo [4 из 5] Готовлю папки, ключ шифрования и базу данных...
venv\Scripts\python.exe run.py init
if errorlevel 1 goto init_fail
echo.
echo [5 из 5] Открываю порт в файрволе Windows...
set "PORT=8080"
for /f "delims=" %%P in ('venv\Scripts\python.exe run.py port') do set "PORT=%%P"
net session >nul 2>nul
if errorlevel 1 goto fw_noadmin
netsh advfirewall firewall delete rule name="Platforma panel" >nul 2>nul
netsh advfirewall firewall add rule name="Platforma panel" dir=in action=allow protocol=TCP localport=%PORT% >nul
echo     Порт %PORT% открыт для входящих подключений.
goto fw_done
:fw_noadmin
echo     Установщик запущен не от имени администратора, поэтому порт %PORT% в файрволе не открывался.
echo     Это нужно, только если панель будут открывать с других компьютеров. Тогда запустите
echo     установить.bat ещё раз: правая кнопка мыши - "Запуск от имени администратора".
:fw_done
echo.
echo ============================================================
echo    Установка завершена.
echo ============================================================
echo.
echo Что дальше:
echo   1. Запустите файл запустить.bat
echo   2. Откройте в браузере адрес, который он покажет.
echo   3. Войдите: логин admin, пароль admin. Система сама попросит придумать новый пароль.
echo   4. Чтобы панель включалась вместе с сервером - запустите автозапуск-включить.bat
echo      (правая кнопка мыши - "Запуск от имени администратора").
echo.
pause
exit /b 0

:no_python
echo.
echo ОШИБКА: на этом компьютере не найден Python версии 3.11 или новее.
echo.
echo ЧТО ДЕЛАТЬ:
echo   1. Сейчас откроется страница https://www.python.org/downloads/windows/
echo   2. Скачайте "Windows installer (64-bit)" для Python 3.11 или новее.
echo   3. При установке ОБЯЗАТЕЛЬНО поставьте галочку "Add python.exe to PATH" внизу первого окна.
echo   4. Когда установка Python закончится - снова запустите установить.bat.
echo.
start "" https://www.python.org/downloads/windows/
pause
exit /b 1

:venv_fail
echo.
echo ОШИБКА: не удалось создать рабочую среду (папку venv).
echo.
echo ЧТО ДЕЛАТЬ:
echo   1. Убедитесь, что программа лежит в простой папке без русских букв и пробелов, например C:\Platforma
echo   2. Убедитесь, что на диске есть свободное место (нужно около 300 МБ).
echo   3. Если папка venv уже есть - удалите её и запустите установить.bat снова.
echo.
pause
exit /b 1

:pip_fail
echo.
echo ОШИБКА: не удалось установить нужные библиотеки.
echo.
echo ЧТО ДЕЛАТЬ:
echo   1. Проверьте, что сервер выходит в интернет: откройте в браузере https://pypi.org
echo   2. Если интернет есть, но выход только через прокси - обратитесь к разработчику или см. ИНСТРУКЦИЮ, раздел "Установка без интернета".
echo   3. Затем запустите установить.bat ещё раз - он продолжит с того места, где остановился.
echo.
pause
exit /b 1

:init_fail
echo.
echo ОШИБКА: не удалось подготовить базу данных и ключ шифрования (см. сообщение выше).
echo.
echo ЧТО ДЕЛАТЬ: прочитайте сообщение выше. Чаще всего помогает: убедиться, что у вашего пользователя Windows
echo есть право записи в эту папку, и что на диске есть свободное место. Затем запустите установить.bat снова.
echo.
pause
exit /b 1
