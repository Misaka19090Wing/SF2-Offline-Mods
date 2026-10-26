@echo off
"%~dp0..\..\runtime\python\python.exe" -I -X utf8 -B "%~dp0tools\install.py" %*
if errorlevel 1 pause
