@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-local.ps1" %*
if errorlevel 1 (
    echo.
    echo The local development launcher failed. See the error above.
    pause
)
endlocal