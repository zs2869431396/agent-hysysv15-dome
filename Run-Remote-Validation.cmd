@echo off
setlocal
cd /d "%~dp0"
echo HYSYS tool layer validation. Console output is ASCII only.
echo Keep HYSYS open and close modal dialogs before starting.
echo Do not run another tool or validation at the same time.
echo Existing user cases will not be modified or closed.
python -m hysys_tools.remote_check
set "VALIDATION_EXIT=%ERRORLEVEL%"
echo.
echo Exit code: %VALIDATION_EXIT%
echo Copy back the acceptance ZIP printed above.
pause
exit /b %VALIDATION_EXIT%
