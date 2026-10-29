@echo off
setlocal
rem Build the Tauri manager window for a release: snipvoice-manager.exe and its
rem third-party notices. Uses the installed Node.js and Rust toolchains; never installs tools.
set "DESKTOP_DIR=%~dp0"
if "%DESKTOP_DIR:~-1%"=="\" set "DESKTOP_DIR=%DESKTOP_DIR:~0,-1%"
set "REPO_DIR=%DESKTOP_DIR%\.."
where npm.cmd >nul 2>nul
if errorlevel 1 goto missing_node
where cargo.exe >nul 2>nul
if errorlevel 1 (
    if exist "%USERPROFILE%\.cargo\bin\cargo.exe" (
        set "PATH=%USERPROFILE%\.cargo\bin;%PATH%"
    ) else (
        goto missing_rust
    )
)
pushd "%DESKTOP_DIR%"
if errorlevel 1 goto directory
rem npm ci installs exactly package-lock.json; tauri build type-checks, bundles and compiles.
call npm.cmd ci --no-audit --no-fund
if errorlevel 1 goto failed
call npm.cmd run tauri build
if errorlevel 1 goto failed
popd
if not exist "%DESKTOP_DIR%\src-tauri\target\release\snipvoice-manager.exe" (
    echo The manager window build finished without snipvoice-manager.exe. 1>&2
    exit /b 1
)
python "%REPO_DIR%\packaging\manager_notices.py" --output "%REPO_DIR%\build\manager-notices.txt"
exit /b %errorlevel%
:failed
popd
echo The manager window build failed. 1>&2
exit /b 1
:directory
echo Cannot enter the desktop folder. Check workspace permissions. 1>&2
exit /b 1
:missing_node
echo Node.js with npm is required to build the manager window. Install it explicitly, then rerun. 1>&2
exit /b 1
:missing_rust
echo The Rust toolchain (cargo) is required to build the manager window. Install it with rustup, then rerun. 1>&2
exit /b 1
