@echo off
rem corpse-flinch uninstaller wrapper (ASCII on purpose; text lives in Python).
setlocal
for %%I in ("%~dp0..\..") do set "CAND=%%~fI"
if exist "%CAND%\runtime\python\python.exe" goto self
if exist "%~1\runtime\python\python.exe" goto given
echo.
echo Could not find the game folder. Run this instead:
echo     ^<game^>\runtime\python\python.exe -X utf8 -B tools\uninstall.py --game ^<game^>
echo.
pause
exit /b 1

:self
"%CAND%\runtime\python\python.exe" -X utf8 -B "%~dp0tools\uninstall.py"
pause
exit /b

:given
"%~1\runtime\python\python.exe" -X utf8 -B "%~dp0tools\uninstall.py" --game "%~1"
pause
exit /b
