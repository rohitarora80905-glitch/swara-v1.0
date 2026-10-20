@echo off
rem Opens the SWARA Server Control window (start/stop the console and UDP relay without a terminal).
cd /d "%~dp0"
set PYW=pythonw
if exist "..\.venv\Scripts\pythonw.exe" set PYW=..\.venv\Scripts\pythonw.exe
start "" "%PYW%" launcher.py
