@echo off
setlocal
cd /d "%~dp0"

echo Installing dependencies...
python -m pip install -q -r requirements.txt pyinstaller
if errorlevel 1 exit /b 1

echo Building ad_connector_composer.exe (GUI)...
python -m PyInstaller --onefile --windowed --name ad_connector_composer ^
  --distpath dist --workpath build --specpath build ^
  ad_connector_composer.py
if errorlevel 1 exit /b 1

copy /Y "dist\ad_connector_composer.exe" "..\ad_connector_composer.exe" >nul
echo.
echo OK: ..\ad_connector_composer.exe
echo Package folder: parent of src\
pause
