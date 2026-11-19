@echo off
cd /d "%~dp0"
if exist "D:\Python\Python313\pythonw.exe" (
  start "" "D:\Python\Python313\pythonw.exe" "%~dp0main.py"
) else (
  py -3.13 "%~dp0main.py"
  if errorlevel 1 pause
)
