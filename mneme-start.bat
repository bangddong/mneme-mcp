@echo off
REM Mneme MCP 서버 기동 스크립트 (부팅 시 자동 실행됨)
REM 파이썬 결정 순서: 1) repo 내 .venv  2) 환경변수 MNEME_PYTHON  3) PATH의 python
cd /d "%~dp0"
set "PYTHON_EXE=python"
if defined MNEME_PYTHON set "PYTHON_EXE=%MNEME_PYTHON%"
if exist "%~dp0.venv\Scripts\python.exe" set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
"%PYTHON_EXE%" -m mneme.server >> "%~dp0memory\server.log" 2>&1
