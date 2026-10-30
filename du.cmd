@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
    py -3 "%SCRIPT_DIR%du.py" %*
) else (
    python "%SCRIPT_DIR%du.py" %*
)
set "EXIT_CODE=%ERRORLEVEL%"
endlocal & exit /b %EXIT_CODE%
