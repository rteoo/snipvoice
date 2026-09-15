@echo off
setlocal EnableExtensions
REM Compile the Windows installer (Setup.exe) from the packaged dist folder.
REM Prerequisite: run build_release.bat first so dist\Snipvoice exists.

set "REPO_DIR=%~dp0"
if "%REPO_DIR:~-1%"=="\" set "REPO_DIR=%REPO_DIR:~0,-1%"
set "ISS=%REPO_DIR%\installer\snipvoice.iss"
set "DIST=%REPO_DIR%\dist\Snipvoice"

if not exist "%DIST%\Snipvoice.exe" (
    echo Packaged app not found: "%DIST%\Snipvoice.exe"
    echo Run build_release.bat first, then re-run this script.
    if not defined SNIPVOICE_BUILD_NONINTERACTIVE pause
    exit /b 1
)

REM Locate the Inno Setup 6 command-line compiler (ISCC.exe).
set "ISCC="
where iscc >nul 2>&1 && set "ISCC=iscc"
if not defined ISCC if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not defined ISCC (
    echo Inno Setup compiler ISCC.exe was not found.
    echo Install Inno Setup 6 ^(free^) from https://jrsoftware.org/isdl.php and re-run.
    if not defined SNIPVOICE_BUILD_NONINTERACTIVE pause
    exit /b 1
)

echo Compiling installer with "%ISCC%"...
"%ISCC%" "%ISS%"
if errorlevel 1 (
    echo Installer compilation failed.
    if not defined SNIPVOICE_BUILD_NONINTERACTIVE pause
    exit /b 1
)

echo.
echo Done. The installer is in installer\Output\
if not defined SNIPVOICE_BUILD_NONINTERACTIVE pause
endlocal
