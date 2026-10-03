@echo off
rem Run every offline test suite. No HYSYS, no COM, no network.
rem Lives in scripts\, so the project root is one level up.
setlocal
cd /d "%~dp0.."

echo Offline test suites. Nothing here touches HYSYS.
echo.

set "FAILED=0"

echo [1/11] tool layer self-check (110 checks)
python -m hysys_tools.selfcheck
if errorlevel 1 set "FAILED=1"
echo.

echo [2/11] tool layer reliability regression (34 tests)
python -m unittest discover -s hysys_tools -t .
if errorlevel 1 set "FAILED=1"
echo.

echo [3/11] model client: fallback chain, throttling, credentials
python -m unittest reactor_agent.test_llm
if errorlevel 1 set "FAILED=1"
echo.

echo [4/11] fact extraction and grounding
python -m unittest reactor_agent.test_extraction
if errorlevel 1 set "FAILED=1"
echo.

echo [5/11] deterministic normalisation
python -m unittest reactor_agent.test_normalize
if errorlevel 1 set "FAILED=1"
echo.

echo [6/11] reactor selection rules
python -m unittest reactor_agent.test_selection
if errorlevel 1 set "FAILED=1"
echo.

echo [7/11] specification compiler
python -m unittest reactor_agent.test_compiler
if errorlevel 1 set "FAILED=1"
echo.

echo [8/11] execution adapter and ledger
python -m unittest reactor_agent.test_adapters
if errorlevel 1 set "FAILED=1"
echo.

echo [9/11] pipeline: refusals, dry runs, resume
python -m unittest reactor_agent.test_pipeline
if errorlevel 1 set "FAILED=1"
echo.

echo [10/11] main graph: routing, pausing, checkpointing
python -m unittest reactor_agent.test_graph
if errorlevel 1 set "FAILED=1"
echo.

echo [11/11] submission packager: exclusions and credential scan
python "%~dp0test_build_submission.py"
if errorlevel 1 set "FAILED=1"
echo.

if "%FAILED%"=="1" (
    echo SOME SUITES FAILED. See the output above.
    pause
    exit /b 1
)
echo ALL SUITES PASSED.
pause
exit /b 0
