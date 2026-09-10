@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\set-api-keys.ps1"
if errorlevel 1 (
  echo.
  echo Key setup did not finish. Do not paste keys into chat.
) else (
  echo.
  echo Local key setup finished. Tell Codex: keys saved.
)
pause
endlocal
