@echo off
setlocal EnableDelayedExpansion

echo ====================================================
echo Building Hytale Chat Windows Client (V7.0 Executable)
echo ====================================================
echo.

REM 1. Verify python is available
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python is not installed or not in PATH.
    pause
    exit /b 1
)

REM 2. Run PyInstaller compilation
echo [1/4] Running PyInstaller...
python -m PyInstaller --onedir --name hytale-tunnel --noconsole --distpath build_dist --workpath build_temp launcher.py
if errorlevel 1 (
    echo [ERROR] PyInstaller compilation failed.
    pause
    exit /b 1
)

REM 3. Prepare target folder Compiled_HyChat
echo [2/4] Preparing Compiled_HyChat folder...
if exist Compiled_HyChat (
    echo Cleaning up old Compiled_HyChat folder...
    rmdir /s /q Compiled_HyChat
)
mkdir Compiled_HyChat

REM 4. Move files to Compiled_HyChat
echo [3/4] Structuring executable files...
xcopy /e /q /y build_dist\hytale-tunnel\* Compiled_HyChat\
if errorlevel 1 (
    echo [ERROR] Failed to move compiled files to Compiled_HyChat.
    pause
    exit /b 1
)

REM 5. Clean up temporary build artifacts
echo [4/4] Cleaning up temporary build files...
rmdir /s /q build_dist
rmdir /s /q build_temp
del /q hytale-tunnel.spec
del /q launcher.py

echo.
echo ====================================================
echo BUILD SUCCESSFUL!
echo Executable: Compiled_HyChat\hytale-tunnel.exe
echo ====================================================
echo.
pause
