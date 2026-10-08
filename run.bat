@echo off
REM One-click launcher for Windows:  double-click run.bat  (or run it from cmd)
REM   1) creates .venv and installs requirements
REM   2) seeds the demo database
REM   3) starts the Streamlit dashboard
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1

where py >nul 2>nul
if %errorlevel%==0 (set "PY=py -3") else (set "PY=python")
%PY% --version >nul 2>nul
if errorlevel 1 goto :nopython

REM --- 1. virtual environment + dependencies ---
if exist ".venv\Scripts\activate.bat" goto :activate
echo ^>^> Creating virtual environment (.venv)
%PY% -m venv .venv
if errorlevel 1 goto :error

:activate
call ".venv\Scripts\activate.bat"
echo ^>^> Installing dependencies (fast if already installed)
python -m pip install --upgrade pip -q
pip install -r requirements.txt -q
if errorlevel 1 goto :error

REM --- config ---
if exist ".env" goto :seed
copy ".env.example" ".env" >nul
echo ^>^> Created .env from .env.example - edit it to add GEMINI_API_KEY and ADMIN_PASSWORD.

REM --- 2. seed demo data ---
:seed
echo ^>^> Seeding demo database
python seed_data.py
if errorlevel 1 goto :error

REM --- 3. launch ---
echo ^>^> Starting HLRN on http://localhost:8501
streamlit run app.py
goto :end

:nopython
echo ERROR: Python 3.10+ was not found. Install it from https://www.python.org/downloads/ and tick "Add to PATH".
pause
exit /b 1

:error
echo.
echo Something failed - see the messages above.
pause
exit /b 1

:end
endlocal
