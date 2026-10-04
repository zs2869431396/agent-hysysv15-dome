@echo off
setlocal
cd /d "%~dp0.."
set "FAILED=0"
echo [1/4] HYSYS offline selfcheck
python -m hysys_tools.selfcheck
if errorlevel 1 set "FAILED=1"
echo [2/4] All tool-layer regression tests
python -m unittest discover -s hysys_tools -t .
if errorlevel 1 set "FAILED=1"
echo [3/4] All agent tests, including Streamlit
python -m unittest discover -s reactor_agent -t .
if errorlevel 1 set "FAILED=1"
echo [4/4] Submission packager
python scripts\test_build_submission.py
if errorlevel 1 set "FAILED=1"
if "%FAILED%"=="1" (
    echo SOME SUITES FAILED. See the output above.
    pause
    exit /b 1
)
echo ALL SUITES PASSED.
pause
exit /b 0
