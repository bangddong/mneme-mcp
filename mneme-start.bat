@echo off
REM Mneme MCP server launcher (auto-run at boot)
REM Python resolution order: 1) repo .venv  2) MNEME_PYTHON env  3) python on PATH
cd /d "%~dp0"
set "PYTHON_EXE=python"
if defined MNEME_PYTHON set "PYTHON_EXE=%MNEME_PYTHON%"
if exist "%~dp0.venv\Scripts\python.exe" set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
"%PYTHON_EXE%" -m mneme.server >> "%~dp0memory\server.log" 2>&1
