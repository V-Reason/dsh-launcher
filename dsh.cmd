@echo off
rem Official DSH CLI shim - resolves the Harness CLI path at runtime
rem (DSH_REPO > vdsh.yaml launcher.repo > local candidates; see dsh_cli.py)
rem the launcher's own python entry is vdsh.cmd
python "%~dp0dsh_cli.py" %*
exit /b %ERRORLEVEL%
