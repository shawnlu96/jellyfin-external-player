@echo off
REM Local Windows build script. Run from project root.
REM Requires: Python 3.12, pip, and the project root contains src\helper.py + src\icon.ico.

setlocal

python -m pip install --upgrade pip
python -m pip install -r requirements.txt pyinstaller

pyinstaller ^
  --onefile ^
  --noconsole ^
  --name JellyfinExternalPlayer ^
  --icon src\icon.ico ^
  --add-data "src\icon.ico;." ^
  src\helper.py

echo.
echo Build done: dist\JellyfinExternalPlayer.exe
endlocal
