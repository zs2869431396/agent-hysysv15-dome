@echo off
rem Compatibility entry point: use the bounded validation runner.
call "%~dp0Run-Remote-Validation.cmd"
exit /b %ERRORLEVEL%
