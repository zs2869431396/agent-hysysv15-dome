@echo off
setlocal
cd /d "%~dp0"
echo Run this on the HYSYS workstation with the intended Python environment active.
echo This drives HYSYS on THIS computer; it does not connect to a remote desktop.
python scripts\validate_native_flow.py
set "NATIVE_EXIT=%ERRORLEVEL%"
pause
exit /b %NATIVE_EXIT%
