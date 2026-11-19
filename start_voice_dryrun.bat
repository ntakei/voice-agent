@echo off
setlocal
cd /d "%~dp0"
rem Use "uv" if it is on PATH, otherwise "python -m uv".
where uv >nul 2>nul && (set "UV=uv") || (set "UV=python -m uv")

rem Voice mode, rehearsal: shows what the agent would do, but does NOT move the mouse or keyboard.
%UV% run voice-agent --voice --dry-run %*
pause
