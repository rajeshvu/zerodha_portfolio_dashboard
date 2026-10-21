@echo off
setlocal
cd /d "%~dp0"

set "PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
    echo Virtual environment not found. Create it and install requirements first.
    echo See README.md in this folder.
    pause
    exit /b 1
)

echo Starting Zerodha login...
"%PYTHON%" "%~dp0auth.py"
if errorlevel 1 (
    echo.
    echo Login did not complete. The dashboard was not started.
    pause
    exit /b 1
)

echo.
echo Starting the portfolio dashboard. Keep this window open while using it.
"%PYTHON%" -m streamlit run "%~dp0dashboard.py"
if errorlevel 1 (
    echo.
    echo The dashboard stopped with an error.
    pause
    exit /b 1
)

endlocal
