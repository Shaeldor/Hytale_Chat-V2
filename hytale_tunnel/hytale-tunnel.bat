@echo off
REM Windows launcher for the Hytale encrypted-chat overlay.
REM Run from the folder that CONTAINS the hytale_tunnel package, or copy this next
REM to it. Adds the parent dir to the path so the package is importable.
setlocal
set "PKG_PARENT=%~dp0.."
python -c "import sys,os; sys.path.insert(0, os.path.abspath(r'%PKG_PARENT%')); from hytale_tunnel.app import main; raise SystemExit(main())" %*
