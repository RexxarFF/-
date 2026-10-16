@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 goto usepython
py -3 run.py
goto done
:usepython
python run.py
:done
pause
