@echo off
rem Starts the PollEdge web app on this computer only and opens it in the browser.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Virtual environment .venv not found - see README, section Installation.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m streamlit run streamlit_app.py --server.address localhost --browser.gatherUsageStats false
