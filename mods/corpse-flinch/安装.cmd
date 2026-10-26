@echo off
rem corpse-flinch installer wrapper.
rem This file is pure ASCII on purpose: cmd.exe parses .cmd bytes with the OEM
rem code page, so every Chinese message lives in tools\install.py instead.
setlocal
set "PY="
set "ARGS="
rem The normal case: this folder is <game>\mods\corpse-flinch.
for %%I in ("%~dp0..\..") do set "CAND=%%~fI"
if exist "%CAND%\runtime\python\python.exe" (
  set "PY=%CAND%\runtime\python\python.exe"
  goto run
)
rem Dragged a game folder onto this file.
if not "%~1"=="" if exist "%~1\runtime\python\python.exe" (
  set "PY=%~1\runtime\python\python.exe"
  set ARGS=--game "%~1"
  goto run
)
rem Unzipped anywhere else: still try, tools\install.py can ask for the folder.
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY goto nopy
:run
%PY% -X utf8 -B "%~dp0tools\install.py" %ARGS%
pause
exit /b

:nopy
echo.
echo Could not find a Python to run tools\install.py.
echo This client ships one at: ^<game^>\runtime\python\python.exe
echo.
echo Unzip corpse-flinch-*.zip into ^<game^>\mods\ , then run this file again,
echo or drag the game folder onto this file.
echo.
pause
exit /b 1
