@echo off
rem Hero Fighter character porter launcher. Keep this file ASCII-only + CRLF.
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   Hero Fighter Character Porter
echo ============================================
echo.
set PYEXE=
where py >nul 2>nul && set PYEXE=py
if "%PYEXE%"=="" ( where python >nul 2>nul && set PYEXE=python )
if "%PYEXE%"=="" (
    echo [ERROR] Python not found. Please install Python 3.8+ and tick
    echo         "Add Python to PATH" during setup.
    echo         Download: https://www.python.org/downloads/
    echo.
    echo Press any key to close...
    pause >nul
    exit /b 1
)
echo Using Python: %PYEXE%
echo.
%PYEXE% -m hfport.cli %*
set RC=%ERRORLEVEL%
echo.
if not "%RC%"=="0" echo [NOTE] Exit code %RC% (non-zero means an error; screenshot the window above).
echo.
echo Press any key to close this window...
pause >nul
