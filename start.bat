@echo off
rem Файл в кодировке cp866 (консоль Windows), не пересохранять в UTF-8
cd /d "%~dp0"

rem --- Python: если нет, ставим через winget (есть в Windows 10/11) ---
set "PY=python"
where python >nul 2>nul && python -c "1" >nul 2>nul && goto :deps
set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if exist "%PY%" goto :deps
echo Python не найден - устанавливаю (1-2 минуты)...
winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
if not exist "%PY%" (
  echo Не получилось установить Python автоматически.
  echo Скачайте его с https://www.python.org/downloads/ и отметьте "Add Python to PATH".
  pause
  exit /b 1
)

:deps
"%PY%" -c "import telethon, python_socks, qrcode, customtkinter, pystray, PIL, anthropic, google.genai, openai" 2>nul || (
  echo Первый запуск: устанавливаю библиотеки...
  "%PY%" -m pip install -r requirements.txt || (pause & exit /b 1)
)

rem pythonw лежит рядом с python.exe и запускает окно без консоли
for /f "delims=" %%i in ('call "%PY%" -c "import sys,os;print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))"') do set "PYW=%%i"
start "" "%PYW%" app.py
