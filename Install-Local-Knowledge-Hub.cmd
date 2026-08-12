@echo off
setlocal
chcp 65001 >nul
title Local Knowledge Hub Installer
echo Installing Local Knowledge Hub for the current Windows user...
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
set "KHUB_INSTALL_RESULT=%errorlevel%"
echo.
if not "%KHUB_INSTALL_RESULT%"=="0" (
  echo Installation failed with exit code %KHUB_INSTALL_RESULT%.
  echo Review the message above, then run this installer again.
) else (
  echo Installation completed successfully.
  echo Restart Codex, Antigravity, and Antigravity IDE before use.
)
echo.
pause
exit /b %KHUB_INSTALL_RESULT%
