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
if not exist build_tools\PresentMon.exe mkdir build_tools
if not exist build_tools\PresentMon.exe (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Invoke-WebRequest -Uri 'https://github.com/GameTechDev/PresentMon/releases/download/v2.6.0/PresentMon-2.6.0-x64.exe' -OutFile 'build_tools/PresentMon.exe'"
  if errorlevel 1 echo PresentMon konnte nicht geladen werden. FPS bleibt ohne Helper nicht verfuegbar.
)
if exist build_tools\PresentMon.exe (
  python -m PyInstaller --noconfirm --clean --onefile --windowed --name StarkinsDashboard --add-binary "build_tools\PresentMon.exe;." --add-data "THIRD-PARTY-NOTICES.txt;." main.py
) else (
  python -m PyInstaller --noconfirm --clean --onefile --windowed --name StarkinsDashboard --add-data "THIRD-PARTY-NOTICES.txt;." main.py
)
echo.
echo Fertig: dist\StarkinsDashboard.exe
pause


