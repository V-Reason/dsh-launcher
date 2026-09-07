@echo off
rem Official DSH CLI shim - forwards to T:\deepseek-harness\apps\cli\lib\bin.js
rem (the launcher's own python entry is vdsh.cmd)
node "T:\deepseek-harness\apps\cli\lib\bin.js" %*
exit /b %ERRORLEVEL%
