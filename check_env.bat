@echo off
setlocal
cd /d "%~dp0"
rem Use "uv" if it is on PATH, otherwise "python -m uv".
where uv >nul 2>nul && (set "UV=uv") || (set "UV=python -m uv")

rem Check the environment (does not move the mouse or keyboard).
%UV% run python scripts\check_env.py %*
pause
