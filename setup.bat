@echo off
setlocal
cd /d "%~dp0"
rem Use "uv" if it is on PATH, otherwise "python -m uv".
where uv >nul 2>nul && (set "UV=uv") || (set "UV=python -m uv")

rem First-time setup: install packages, create .env, download the speech model, check the environment.
%UV% --version >nul 2>nul
if errorlevel 1 (
  echo [ERROR] uv was not found. Install it first:  python -m pip install --user uv
  pause
  exit /b 1
)
%UV% sync || goto :fail
if not exist .env copy .env.example .env >nul
%UV% run python scripts\download_models.py || goto :fail
%UV% run python scripts\check_env.py
echo.
echo Setup finished. Start with start_voice.bat (voice) or start_text.bat (text).
pause
exit /b 0

:fail
echo [ERROR] Setup failed. See the messages above.
pause
exit /b 1
