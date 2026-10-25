@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-dashboard.ps1" %*
set "dashboardExit=%errorlevel%"
if not "%dashboardExit%"=="0" pause
exit /b %dashboardExit%
