@echo off
rem Live PAPER day trading on this PC (never places real orders). Usage: run_live.bat india   or   run_live.bat us
rem Reads credentials from .env in this folder (copy env.template to .env and fill it in; .env is never committed).
rem Keep the PC awake for the whole session (NSE: 10:45 PM - 5:00 AM Central).
setlocal
set PY=%USERPROFILE%\.venvs\trading\Scripts\python.exe
if not exist "%PY%" set PY=python
cd /d "%~dp0"
if not exist .env (echo .env not found - copy env.template to .env and fill it in & pause & exit /b 1)
set MKT=%1
if "%MKT%"=="" set /p MKT=Market [india/us]: 
"%PY%" engine.py %MKT% --watch
pause
