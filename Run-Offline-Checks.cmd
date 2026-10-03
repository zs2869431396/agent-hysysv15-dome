@echo off
setlocal
cd /d "%~dp0"
echo Offline checks only. No COM or HYSYS calls.
python -m hysys_tools.remote_check --offline
set "VALIDATION_EXIT=%ERRORLEVEL%"
pause
exit /b %VALIDATION_EXIT%
