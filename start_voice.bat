@echo off
setlocal
cd /d "%~dp0"
rem Use "uv" if it is on PATH, otherwise "python -m uv".
where uv >nul 2>nul && (set "UV=uv") || (set "UV=python -m uv")

rem Voice mode: hold F9 and speak. The agent REALLY moves the mouse and keyboard.
rem Stop: Esc (or move the mouse to a screen corner). Quit: Ctrl+C.
%UV% run voice-agent --voice --live %*
pause
