@echo off
REM One-time setup for the Hytale chat tunnel on Windows.
REM Installs the two Python packages the overlay needs. Run by double-clicking.
setlocal EnableDelayedExpansion
set "PYEXE="

where py >nul 2>nul && set "PYEXE=py"
if not defined PYEXE (
  for %%D in ("%LOCALAPPDATA%\Programs\Python" "%PROGRAMFILES%\Python313" "%PROGRAMFILES%\Python312" "%PROGRAMFILES%\Python311") do (
    if exist "%%~D" (
      for /f "delims=" %%P in ('dir /b /s "%%~D\python.exe" 2^>nul') do (
        if not defined PYEXE set "PYEXE=%%P"
      )
    )
  )
)
if not defined PYEXE set "PYEXE=python"

echo Using Python: %PYEXE%
"%PYEXE%" --version
if errorlevel 1 (
  echo.
  echo Python was not found. Install it first:
  echo   1. Get it from https://www.python.org/downloads/  ^(tick "Add python.exe to PATH"^)
  echo   2. Re-run this setup-windows.bat
  echo.
  pause
  exit /b 1
)

echo.
echo Installing PyQt6 + cryptography + frida ...
"%PYEXE%" -m pip install --upgrade pip
"%PYEXE%" -m pip install pyqt6 cryptography frida
if errorlevel 1 (
  echo.
  echo Install failed. Check your internet connection and try again.
  pause
  exit /b 1
)

echo.
echo Done. Next:
echo   1. Set up a shared key with your friend ^(both store the SAME key^):
echo        "%PYEXE%" hytalecrypt setkey ^<theirname^> ^<key^>
echo   2. Start the overlay:
echo        hytale_tunnel\hytale-tunnel.bat -r ^<friend^>
echo.
pause
