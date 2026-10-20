@echo off
rem Launch the SWARA web console (http://localhost:8000) and the hosted UDP relay (udp:9000).
cd /d "%~dp0"
set PY=python
if exist "..\.venv\Scripts\python.exe" set PY=..\.venv\Scripts\python.exe
start "" http://localhost:8000/
"%PY%" api_server.py --host 0.0.0.0 --port 8000 --https-port 8443
