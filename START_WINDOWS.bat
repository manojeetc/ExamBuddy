@echo off
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  set SYSTEM_PY=py -3
) else (
  set SYSTEM_PY=python
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating a private Python environment for Exam Practice...
  %SYSTEM_PY% -m venv .venv
)

if exist "dist\AMCExamPractice\AMCExamPractice.exe" (
  start "AMC Exam Practice" "dist\AMCExamPractice\AMCExamPractice.exe"
  exit /b 0
)

set PY=.venv\Scripts\python.exe
%PY% -c "import flask, flaskwebgui, fitz, PIL" >nul 2>nul
if errorlevel 1 (
  echo Installing Flask GUI dependencies into the private environment...
  %PY% -m pip install -r requirements.txt
)

%PY% launcher.py
pause
