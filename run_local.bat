@echo off
REM ============================================================
REM  Investment Banking Agentic AI Platform - local launcher
REM  Double-click this file to start the app on your desktop.
REM ============================================================
cd /d "%~dp0"

echo.
echo   Starting the Investment Banking Agentic AI Platform...
echo.
echo   Once it says "Application startup complete", open:
echo     http://127.0.0.1:8000/dashboard (Live analytics dashboard - real data)
echo     http://127.0.0.1:8000/overview  (Overview dashboard - projected targets)
echo     http://127.0.0.1:8000/docs      (API explorer)
echo     http://127.0.0.1:8000/reviewer  (Reviewer dashboard)
echo.
echo   Leave this window OPEN while you use the app.
echo   Press Ctrl+C (or close this window) to stop it.
echo.

".venv\Scripts\python.exe" -m uvicorn app.api:app --host 127.0.0.1 --port 8000

echo.
echo   Server stopped. Press any key to close this window.
pause >nul
