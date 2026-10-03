@echo off
rem ============================================================================
rem  Local web interface for the agent:  python -m reactor_agent.web
rem
rem  Opens a page on 127.0.0.1 only. Nothing is sent anywhere except the model
rem  endpoint you configure in the page itself.
rem
rem  The model key is NOT needed in this window: type it into the page, where it
rem  is kept in the server process memory and never written to disk. If you prefer
rem  not to type it every time, put TR_KEY=... in a `.env` file in this folder;
rem  `.env` is git-ignored and is not part of the submission package.
rem
rem  Usage:  Run-Agent-UI.cmd            (port 8765)
rem          Run-Agent-UI.cmd 8899       (another port)
rem ============================================================================
setlocal
cd /d "%~dp0"

set "PORT=%~1"
if "%PORT%"=="" set "PORT=8765"

echo ============================================================
echo  Agent web interface  -  http://127.0.0.1:%PORT%
echo ============================================================
echo.
echo  Press Ctrl+C in this window to stop the server.
echo.

python -c "import pydantic, langgraph; print('agent dependencies OK')" || goto :nodeps
echo.

start "" "http://127.0.0.1:%PORT%"
python -m reactor_agent.web --port %PORT%
set "CODE=%ERRORLEVEL%"
echo.
echo Server stopped (exit code %CODE%).
pause
exit /b %CODE%

:nodeps
echo.
echo ERROR: pydantic or langgraph is missing.
echo   python -m pip install -r requirements.txt
pause
exit /b 3
