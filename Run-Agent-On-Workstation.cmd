@echo off
rem ============================================================================
rem  Agent end-to-end on a workstation that HAS HYSYS.
rem
rem  This is the one verification that cannot be done on the development machine,
rem  because it has no HYSYS and no pywin32. Run this on the workstation.
rem
rem  Before running:
rem    1. install the dependencies, including pywin32:
rem         conda activate hysys-agent
rem         python -m pip install -r requirements.txt
rem         python -m pip install pywin32
rem    2. start HYSYS and dismiss any dialogs. Close leftover cases: the tool
rem       layer refuses to run against a session it does not own, and previous
rem       runs have failed because 96 stale cases were left open.
rem    3. set the model credentials IN THIS WINDOW (they are never written to disk):
rem         set TR_KEY=<your key>
rem       (TR_BASE and TR_MODEL have working defaults.)
rem
rem  What it does: drives the tool layer through the agent, end to end, for
rem  toluene and steam reforming, with a dry run of gasification first so the
rem  refusal is visible. Every case gets its own fresh directory.
rem ============================================================================
setlocal
cd /d "%~dp0"

echo ============================================================
echo  Agent end-to-end on this workstation
echo ============================================================
echo.

if "%TR_KEY%"=="" (
    echo ERROR: TR_KEY is not set in this window.
    echo.
    echo   set TR_KEY=^<your key^>
    echo.
    echo The key is read from the environment only and is never written to a file.
    pause
    exit /b 2
)

echo Checking the Python environment...
python -c "import sys; print('  python', sys.version.split()[0])"
python -c "import pydantic, langgraph; print('  agent dependencies OK')" || goto :nodeps
python -c "import win32com.client; print('  pywin32 OK')" || goto :nopywin32
echo.

set "RUN=agent-runs\workstation-%DATE:~0,4%%DATE:~5,2%%DATE:~8,2%-%TIME:~0,2%%TIME:~3,2%%TIME:~6,2%"
set "RUN=%RUN: =0%"

echo Run folder: %RUN%
echo.

echo ------------------------------------------------------------
echo  [1/4] gasification: dry run, must REFUSE and execute nothing
echo ------------------------------------------------------------
python -m reactor_agent --graph --scenario gasification --out "%RUN%\gasification-dryrun"
echo   ^(exit code 3 = paused for clarification, which is correct^)
echo.

echo ------------------------------------------------------------
echo  [2/4] toluene: REAL run on HYSYS
echo ------------------------------------------------------------
python -m reactor_agent --scenario toluene --execute --out "%RUN%\toluene"
if errorlevel 1 echo   ^(non-zero exit; see the output above^)
echo.

echo ------------------------------------------------------------
echo  [3/4] steam reforming: REAL run, two operating cases
echo ------------------------------------------------------------
python -m reactor_agent --scenario smr --execute --out "%RUN%\smr"
if errorlevel 1 echo   ^(non-zero exit; see the output above^)
echo.

echo ------------------------------------------------------------
echo  [4/4] gasification: REAL run, must still refuse
echo ------------------------------------------------------------
python -m reactor_agent --scenario gasification --execute --out "%RUN%\gasification-execute"
echo   ^(HYSYS must NOT have been touched: no .hsc should exist^)
echo.

echo ============================================================
echo  Expected results, from the accepted tool-layer baseline
echo ============================================================
echo   toluene   conversion 49.99999999999999%%
echo             outlet: Toluene 54.2648, Benzene 27.1324, xylenes 9.0441 each
echo   smr 710C  CH4 54.035809%%, H2 1942.8123 kmol/h, duty 39988.6148 kW
echo   smr 600C  CH4 30.352385%%, H2 1163.6704 kmol/h, duty 20159.4980 kW
echo   gasification: refused, 0 case files created
echo.
echo Results and specs are under: %RUN%
echo Send back this whole folder, or the zip if one was produced.
echo.
pause
exit /b 0

:nodeps
echo.
echo ERROR: pydantic or langgraph is missing.
echo   python -m pip install -r requirements.txt
pause
exit /b 3

:nopywin32
echo.
echo ERROR: pywin32 is missing, so HYSYS cannot be driven from Python.
echo   python -m pip install pywin32
echo   (requirements.txt leaves it commented out, because the development
echo    machine has no HYSYS and installing it there would only make the
echo    environment look ready when it is not.)
pause
exit /b 4
