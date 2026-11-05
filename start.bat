@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
    set "PYTHON=py -3"
) else (
    set "PYTHON=python"
)
%PYTHON% --version
if errorlevel 1 (
    echo Python не найден. Установите Python 3 и включите Add Python to PATH.
    pause
    exit /b 1
)
%PYTHON% -m pip install -r requirements.txt
if errorlevel 1 (
    echo Ошибка установки библиотек. Проверьте интернет и Python.
    pause
    exit /b 1
)
%PYTHON% main.py
if errorlevel 1 echo Программа завершилась с ошибкой.
pause
