@echo off
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python 3.10+ wird benoetigt: https://www.python.org/downloads/
  echo Beim Installieren "Add python.exe to PATH" anhaken, dann build.bat erneut starten.
  pause & exit /b 1
)
python -m venv .venv
call .venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt pyinstaller==6.22.3
python -m PyInstaller --noconfirm --clean --onefile --windowed --name StarkinsDashboard main.py
echo.
echo Fertig: dist\StarkinsDashboard.exe
pause

