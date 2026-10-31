@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
  echo Python 3 is required to build the Windows application.
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" py -3 -m venv .venv
set PY=.venv\Scripts\python.exe

echo Installing application and packaging dependencies...
%PY% -m pip install -r requirements.txt
%PY% -m pip install "pyinstaller>=6,<7"

echo Building AMC Exam Practice...
%PY% -m PyInstaller --noconfirm --clean --onedir --windowed --name AMCExamPractice ^
  --add-data "templates;templates" ^
  --add-data "static;static" ^
  launcher.py

if errorlevel 1 (
  echo Build failed.
  exit /b 1
)

rem The app resolves these next to the exe (not in _internal), and they must stay writable.
for %%D in (QuestionBank AnswerLog Response supabase) do (
  if exist "%%D" robocopy "%%D" "dist\AMCExamPractice\%%D" /E /NFL /NDL /NJH /NJS /NP >nul
)
if exist ".env" copy /Y ".env" "dist\AMCExamPractice\.env" >nul

echo.
echo Build complete: dist\AMCExamPractice\AMCExamPractice.exe
echo Copy the entire dist\AMCExamPractice folder when distributing the app.
endlocal
