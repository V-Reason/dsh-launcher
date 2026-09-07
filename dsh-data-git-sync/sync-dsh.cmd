@echo off
setlocal
rem ASCII-only launcher: all user-facing text lives in sync-dsh.ps1 (UTF-8 BOM).
rem Double-click (no args) -> interactive menu inside the script.
rem With args (e.g. "sync-dsh.cmd push") -> pass through.
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0sync-dsh.ps1" %*
if "%~1"=="" pause
endlocal
