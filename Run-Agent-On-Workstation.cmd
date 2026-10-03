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
rem  toluene, steam reforming (two cases) and gasification on the accepted
rem  saturated-carbon route. Every case gets its own fresh directory, and the
rem  last step is an interactive dry run so the two gasification questions and
rem  their suggested answers are visible on screen.
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
echo  [1/4] toluene: REAL run on HYSYS (Conversion, adiabatic)
echo ------------------------------------------------------------
python -m reactor_agent --scenario toluene --execute --accept-defaults --out "%RUN%\toluene"
if errorlevel 1 echo   ^(non-zero exit; see the output above^)
echo.

echo ------------------------------------------------------------
echo  [2/4] steam reforming: REAL run (Equilibrium, two cases)
echo ------------------------------------------------------------
python -m reactor_agent --scenario smr --execute --accept-defaults --out "%RUN%\smr"
if errorlevel 1 echo   ^(non-zero exit; see the output above^)
echo.

echo ------------------------------------------------------------
echo  [3/4] gasification: REAL run (Gibbs + saturated carbon)
echo ------------------------------------------------------------
python -m reactor_agent --scenario gasification --execute --accept-defaults --out "%RUN%\gasification"
if errorlevel 1 echo   ^(non-zero exit; see the output above^)
echo.

echo ------------------------------------------------------------
echo  [4/4] gasification: dry run, INTERACTIVE
echo        Two questions appear; press Enter twice to accept the
echo        suggested answers. Nothing is simulated in this step.
echo ------------------------------------------------------------
python -m reactor_agent --scenario gasification --out "%RUN%\gasification-interactive"
echo   ^(exit code 0 = the answers were accepted and the spec was compiled^)
echo.

echo ============================================================
echo  Expected results
echo ============================================================
echo   toluene   conversion 49.99999999999999%%
echo             outlet: Toluene 54.2648, Benzene 27.1324, xylenes 9.0441 each
echo             the same numbers as the historical baseline, digit for digit
echo   smr       Equilibrium, both cases PASS
echo             CH4 conversion within 0.5 percentage points of the Gibbs history
echo               (710C 54.035809%%, 600C 30.352385%%)
echo             duty within 1%% of 39988.6148 kW (710C) / 20159.4980 kW (600C)
echo             a Q/K line per reaction, verdict PASS
echo             a two-temperature comparison with its reading
echo   gasification  saturated-carbon route PASS
echo             CO yield about 40.14%%, carbon conversion about 41.16%%
echo             external duty about 84656 kW
echo             the LIQUID stream is labelled as solid carbon
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
