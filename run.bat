@echo off
cd /d "%~dp0"
if not exist .venv (
    py -3.13 -m venv .venv
    .venv\Scripts\python -m pip install -r requirements.txt
)
.venv\Scripts\python.exe -c "import tkinter" 2>nul || (echo .venv has no tkinter: delete the .venv folder and run again & pause & exit /b 1)
start "" .venv\Scripts\pythonw.exe main.py
