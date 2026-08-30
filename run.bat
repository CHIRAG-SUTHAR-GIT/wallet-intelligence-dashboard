@echo off
REM ===================================================================
REM  Wallet Intelligence Dashboard - launcher
REM ===================================================================

setlocal enabledelayedexpansion

cd /d "%~dp0"

REM -- Prefer the py launcher, fall back to python on PATH ------------
set "PY=py -3"
py -3 -c "import sys" >nul 2>&1
if errorlevel 1 (
    set "PY=python"
    python -c "import sys" >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] Python was not found on this machine.
        echo         Install it from https://www.python.org/downloads/
        echo.
        pause
        exit /b 1
    )
)

if not exist "app.py" (
    echo [ERROR] app.py not found in %CD%
    echo.
    pause
    exit /b 1
)

REM -- Check the libraries the app imports ----------------------------
%PY% -c "import streamlit, pandas, numpy, altair, networkx, xlsxwriter, openpyxl" >nul 2>&1

if errorlevel 1 (
    echo Some required packages are missing.
    echo.
    echo   streamlit  pandas  numpy  altair  networkx  xlsxwriter  openpyxl
    echo.
    set /p INSTALL="Install them now with pip? [Y/N] "
    if /i not "!INSTALL!"=="Y" (
        echo Cancelled.
        pause
        exit /b 1
    )
    echo.
    echo Installing...
    %PY% -m pip install --upgrade pip
    %PY% -m pip install streamlit pandas numpy altair networkx xlsxwriter openpyxl
    if errorlevel 1 (
        echo.
        echo [ERROR] Installation failed.
        pause
        exit /b 1
    )
)

echo.
echo Starting the Wallet Intelligence Dashboard...
echo Your browser should open automatically. Close this window to stop.
echo.

%PY% -m streamlit run app.py

REM Streamlit exiting with an error should not close the window
REM before the message can be read.
if errorlevel 1 (
    echo.
    echo [ERROR] Streamlit exited unexpectedly. See the messages above.
    pause
)

endlocal
