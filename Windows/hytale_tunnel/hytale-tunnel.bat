@echo off
REM Windows launcher for the Hytale encrypted-chat overlay.
REM Run from the folder that CONTAINS the hytale_tunnel package, or copy this next
REM to it. Locates a real Python (avoiding the Microsoft Store stub) and starts the
REM overlay with the package importable.
setlocal EnableDelayedExpansion
set "PKG_PARENT=%~dp0.."
set "PYEXE="

REM 1) the "py" launcher (python.org installs it; the Store stub does NOT provide it,
REM    so finding py.exe means we have a real interpreter).
where py >nul 2>nul && set "PYEXE=py"

REM 2) probe the usual install dirs for a real python.exe. This dodges the Store stub
REM    (a fake python.exe in WindowsApps that just opens the Store) and a stale PATH.
if not defined PYEXE (
  for %%D in ("%LOCALAPPDATA%\Programs\Python" "%PROGRAMFILES%\Python313" "%PROGRAMFILES%\Python312" "%PROGRAMFILES%\Python311") do (
    if exist "%%~D" (
      for /f "delims=" %%P in ('dir /b /s "%%~D\python.exe" 2^>nul') do (
        if not defined PYEXE set "PYEXE=%%P"
      )
    )
  )
)

REM 3) last resort: plain "python" on PATH (may be the Store stub).
if not defined PYEXE set "PYEXE=python"

"%PYEXE%" -c "import sys,os; sys.path.insert(0, os.path.abspath(r'%PKG_PARENT%')); from hytale_tunnel.app import main; raise SystemExit(main())" %* 2> "%PKG_PARENT%\crash_log.txt"
if errorlevel 1 (
  echo.
  echo Could not start. If Python is missing or the packages aren't installed,
  echo run setup-windows.bat once first ^(installs PyQt6 + cryptography^).
  pause
)
