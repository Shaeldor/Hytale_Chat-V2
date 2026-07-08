@echo off
REM Convenience wrapper for the hytalecrypt CLI on Windows.
REM Usage examples (run from this folder):
REM   hcrypt genkey
REM   hcrypt setkey Revenir <key>
REM   hcrypt senc Revenir "secret message"
REM   hcrypt sdec HX1....
REM   hcrypt list
REM Locates a real Python past the Microsoft Store stub, same as the launcher.
setlocal EnableDelayedExpansion
set "HERE=%~dp0"
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

"%PYEXE%" "%HERE%hytalecrypt" %*
