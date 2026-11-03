@echo off
rem Khoi dong Long Video Maker va mo trinh duyet (Windows)
cd /d "%~dp0"
set PORT=8501

netstat -ano | findstr /r /c:":%PORT% .*LISTENING" >nul
if errorlevel 1 (
  start "Long Video Maker" /min cmd /c ".venv\Scripts\streamlit.exe run app.py --server.port %PORT% --server.headless true > streamlit.log 2>&1"
  for /l %%i in (1,1,30) do (
    netstat -ano | findstr /r /c:":%PORT% .*LISTENING" >nul && goto :open
    ping -n 2 127.0.0.1 >nul
  )
)
:open
start "" "http://localhost:%PORT%"
