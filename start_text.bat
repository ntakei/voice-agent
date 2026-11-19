@echo off
setlocal
cd /d "%~dp0"
rem Use "uv" if it is on PATH, otherwise "python -m uv".
where uv >nul 2>nul && (set "UV=uv") || (set "UV=python -m uv")

rem Text mode: type a task at the prompt. The agent REALLY moves the mouse and keyboard.
rem An empty line quits. Stop a running task with Esc.
%UV% run voice-agent --live %*
pause
