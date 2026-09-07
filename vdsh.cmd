@echo off
rem vdsh launcher shim - CMD / Windows PowerShell 5.1 / PowerShell 7
rem renamed from dsh to avoid conflict with the official dsh CLI
python "%~dp0vdsh_launcher.py" %*
exit /b %ERRORLEVEL%
