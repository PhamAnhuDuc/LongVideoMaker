@echo off
rem Khoi dong Long Video Maker va mo trong cua so Chrome rieng (Windows)
cd /d "%~dp0"
set PORT=8501
set URL=http://localhost:%PORT%

netstat -ano | findstr /r /c:":%PORT% .*LISTENING" >nul
if errorlevel 1 (
  start "Long Video Maker" /min cmd /c ".venv\Scripts\streamlit.exe run app.py --server.port %PORT% --server.headless true > streamlit.log 2>&1"
  for /l %%i in (1,1,30) do (
    netstat -ano | findstr /r /c:":%PORT% .*LISTENING" >nul && goto :open
    ping -n 2 127.0.0.1 >nul
  )
)
:open
set CHROME=
if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not defined CHROME if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" set "CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not defined CHROME if exist "%LocalAppData%\Google\Chrome\Application\chrome.exe" set "CHROME=%LocalAppData%\Google\Chrome\Application\chrome.exe"

rem --app = cua so rieng gon nhu phan mem, khong thanh dia chi, khong tab
rem Luu y: khong viet dau ngoac trong khoi if ben duoi, cmd se hieu nham la ket thuc khoi
if not defined CHROME goto :default
start "" "%CHROME%" --app=%URL% --window-size=1400,900
goto :eof
:default
start "" "%URL%"
