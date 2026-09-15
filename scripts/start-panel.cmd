@echo off
REM Start everything in the foreground with a console (handy for debugging).
setlocal
python "%~dp0..\run.py" --foreground
