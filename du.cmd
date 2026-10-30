@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
    py -3 "%SCRIPT_DIR%du.py" %*
) else if exist "C:\Users\Robert\AppData\Local\Python\pythoncore-3.14-64\python.exe" (
    "C:\Users\Robert\AppData\Local\Python\pythoncore-3.14-64\python.exe" "%SCRIPT_DIR%du.py" %*
) else (
    python "%SCRIPT_DIR%du.py" %*
)
set "EXIT_CODE=%ERRORLEVEL%"
endlocal & exit /b %EXIT_CODE%
