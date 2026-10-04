@echo off
setlocal
cd /d "%~dp0"
set "PORT=%~1"
if "%PORT%"=="" set "PORT=8501"
set "AGENT_PYTHON="
if defined HYSYS_AGENT_PYTHON if exist "%HYSYS_AGENT_PYTHON%" set "AGENT_PYTHON=%HYSYS_AGENT_PYTHON%"
if not defined AGENT_PYTHON if exist "%USERPROFILE%\Miniconda3\envs\hysys-agent\python.exe" set "AGENT_PYTHON=%USERPROFILE%\Miniconda3\envs\hysys-agent\python.exe"
if not defined AGENT_PYTHON if exist "%USERPROFILE%\anaconda3\envs\hysys-agent\python.exe" set "AGENT_PYTHON=%USERPROFILE%\anaconda3\envs\hysys-agent\python.exe"
if not defined AGENT_PYTHON if defined CONDA_PREFIX if exist "%CONDA_PREFIX%\python.exe" set "AGENT_PYTHON=%CONDA_PREFIX%\python.exe"
if not defined AGENT_PYTHON set "AGENT_PYTHON=python"
echo HYSYS Chat - http://127.0.0.1:%PORT%
echo Press Ctrl+C to stop.
"%AGENT_PYTHON%" -c "import streamlit, langgraph, pydantic" >nul 2>&1
if errorlevel 1 goto :missing
"%AGENT_PYTHON%" -m streamlit run reactor_agent\streamlit_app.py --server.address 127.0.0.1 --server.port %PORT% --server.headless false --browser.gatherUsageStats false
set "CODE=%ERRORLEVEL%"
pause
exit /b %CODE%
:missing
echo Dependencies missing. In Anaconda Prompt, run:
echo conda activate hysys-agent
echo python -m pip install -r requirements.txt
echo If needed, set HYSYS_AGENT_PYTHON to your environment's python.exe.
pause
exit /b 3
