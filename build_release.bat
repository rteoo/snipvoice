@echo off
setlocal EnableExtensions
REM Build a new packaged release using PyInstaller.
REM The build always stages into a temporary dist folder first so an existing
REM packaged app stays intact unless the new build succeeds.

set "REPO_DIR=%~dp0"
if "%REPO_DIR:~-1%"=="\" set "REPO_DIR=%REPO_DIR:~0,-1%"
set "DIST_ROOT=%REPO_DIR%\dist"
set "TARGET_DIR=%DIST_ROOT%\Snipvoice"
set "TARGET_EXE=%TARGET_DIR%\Snipvoice.exe"
set "STAGING_ROOT=%TEMP%\snipvoice_staging_%RANDOM%%RANDOM%"
set "STAGING_DIR=%STAGING_ROOT%\Snipvoice"
set "PREVIOUS_DIR=%DIST_ROOT%\Snipvoice.previous"
set "WORK_ROOT=%TEMP%\snipvoice_pyinstaller_%RANDOM%%RANDOM%"
set "WORK_DIR=%WORK_ROOT%\build"
set "STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "SHORTCUT_PATH=%STARTUP_DIR%\Snipvoice.lnk"

if exist "%PREVIOUS_DIR%" (
    echo A previous rollback copy exists: "%PREVIOUS_DIR%"
    echo Resolve this recovery copy before rebuilding. No files were deleted.
    pause
    exit /b 1
)

tasklist /FI "IMAGENAME eq Snipvoice.exe" 2>nul | find /I "Snipvoice.exe" >nul
if not errorlevel 1 (
    echo "Snipvoice.exe" is currently running.
    echo Close the packaged app before rebuilding dist so the update can replace the old folder safely.
    pause
    exit /b 1
)

if exist "%TARGET_DIR%" (
    echo Existing dist detected: "%TARGET_DIR%"
) else (
    echo No existing dist found. A fresh packaged release will be created.
)

REM Snipvoice owns ~/.snipvoice; it never copies another application's data.
if exist "%STAGING_ROOT%" (
    attrib -r "%STAGING_ROOT%\*.*" /s /d >nul 2>&1
    rmdir /s /q "%STAGING_ROOT%" >nul 2>&1
)

set "VOICE_COLLECT_ARGS="
python -c "import tkinter; tkinter.Tcl()" >nul 2>&1
if errorlevel 1 (
    echo The selected Python installation cannot initialize Tcl/Tk.
    echo Repair or select a Python installation with working Tcl/Tk before packaging.
    goto cleanup_and_fail
)
python -c "import sounddevice, soxr, transcribe_cpp, transcribe_cpp_native" >nul 2>&1
if errorlevel 1 (
    echo Voice release dependencies are missing.
    echo Install them with: python -m pip install -r source\requirements-voice.txt
    goto cleanup_and_fail
)
set "VOICE_COLLECT_ARGS=--collect-all sounddevice --collect-all soxr --copy-metadata soxr --collect-all transcribe_cpp --collect-all transcribe_cpp_native"

python -m PyInstaller --noconfirm --clean --windowed --onedir --distpath "%STAGING_ROOT%" --workpath "%WORK_DIR%" --specpath "%REPO_DIR%" --name "Snipvoice" --icon "%REPO_DIR%\source\snipvoice.ico" --add-data "%REPO_DIR%\source\snipvoice.ico;." --add-data "%REPO_DIR%\THIRD_PARTY_NOTICES.md;." --add-data "%REPO_DIR%\LICENSE;." --hidden-import pystray._win32 %VOICE_COLLECT_ARGS% --exclude-module torch --exclude-module torchvision --exclude-module torchaudio --exclude-module cv2 --exclude-module transformers --exclude-module onnxruntime --exclude-module scipy "%REPO_DIR%\source\snipvoice.pyw"
if errorlevel 1 (
    echo.
    echo Packaging failed. The existing dist was left unchanged.
    goto cleanup_and_fail
)

if not exist "%STAGING_DIR%" (
    echo Packaging failed: staged dist was not created.
    goto cleanup_and_fail
)

start "" /wait "%STAGING_DIR%\Snipvoice.exe" --voice-runtime-probe
if errorlevel 1 (
    echo Packaging failed: the staged voice runtime probe did not pass.
    goto cleanup_and_fail
)

REM Snipvoice reads and writes user data only in its independent data directory.

call :promote_staged_release
if errorlevel 1 (
    goto cleanup_and_fail
)

if exist "%SHORTCUT_PATH%" (
    echo Startup shortcut already exists. Skipping shortcut prompt.
    goto finish
)

echo.
set /p "ADD_STARTUP_SHORTCUT=Add a Startup shortcut for Snipvoice? [Y/N]: "
if /I "%ADD_STARTUP_SHORTCUT%"=="Y" goto install_startup
if /I "%ADD_STARTUP_SHORTCUT%"=="YES" goto install_startup
goto finish

:install_startup
if not exist "%TARGET_EXE%" (
    echo Packaged executable not found: "%TARGET_EXE%"
    goto finish
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "$ws = New-Object -ComObject WScript.Shell; $shortcut = $ws.CreateShortcut('%SHORTCUT_PATH%'); $shortcut.TargetPath = '%TARGET_EXE%'; $shortcut.WorkingDirectory = '%TARGET_DIR%'; $shortcut.IconLocation = '%TARGET_EXE%,0'; $shortcut.Save()"
if errorlevel 1 (
    echo Failed to create the Startup shortcut.
) else (
    echo Startup shortcut created: "%SHORTCUT_PATH%"
)

:finish
echo Packaging complete. The release folder is in dist\"Snipvoice"\
echo User data lives in "%USERPROFILE%\.snipvoice" ^(override with SNIPVOICE_HOME^).
goto cleanup_and_exit

:promote_staged_release
if exist "%TARGET_DIR%" (
    echo Replacing the previous dist with the new packaged release...
    move "%TARGET_DIR%" "%PREVIOUS_DIR%" >nul
    if errorlevel 1 (
        echo Failed to move the existing dist out of the way.
        echo Close any running "Snipvoice.exe" instance and try again.
        exit /b 1
    )
)

if not exist "%DIST_ROOT%" mkdir "%DIST_ROOT%"
call robocopy "%STAGING_DIR%" "%TARGET_DIR%" /e /j /nfl /ndl /njh /njs /r:0 /w:0 >nul
if errorlevel 8 goto promote_rollback
if not exist "%TARGET_EXE%" goto promote_rollback

if exist "%PREVIOUS_DIR%" (
    attrib -r "%PREVIOUS_DIR%\*.*" /s /d >nul 2>&1
    rmdir /s /q "%PREVIOUS_DIR%" >nul 2>&1
    if exist "%PREVIOUS_DIR%" (
        echo Failed to discard the temporary rollback copy.
        echo The new dist is present, but the previous package was retained for recovery.
        exit /b 1
    )
)
exit /b 0

:promote_rollback
echo Failed to promote the new staged dist into place.
if exist "%PREVIOUS_DIR%" (
    echo Attempting to restore previous dist...
    if exist "%TARGET_DIR%" (
        attrib -r "%TARGET_DIR%\*.*" /s /d >nul 2>&1
        rmdir /s /q "%TARGET_DIR%" >nul 2>&1
        if exist "%TARGET_DIR%" (
            echo Failed to clear the partial new dist before restoring.
            echo Previous package retained at "%PREVIOUS_DIR%".
            exit /b 1
        )
    )
    call robocopy "%PREVIOUS_DIR%" "%TARGET_DIR%" /e /j /nfl /ndl /njh /njs /r:0 /w:0 >nul
    if errorlevel 8 (
        echo Failed to restore the previous dist.
        echo Previous package retained at "%PREVIOUS_DIR%".
        exit /b 1
    )
    if not exist "%TARGET_EXE%" (
        echo Restored dist is missing Snipvoice.exe.
        echo Previous package retained at "%PREVIOUS_DIR%".
        exit /b 1
    )
    echo Previous dist restored; rollback copy retained at "%PREVIOUS_DIR%".
) else (
    if exist "%TARGET_DIR%" (
        attrib -r "%TARGET_DIR%\*.*" /s /d >nul 2>&1
        rmdir /s /q "%TARGET_DIR%" >nul 2>&1
        if exist "%TARGET_DIR%" (
            echo Failed to clear the incomplete dist.
            exit /b 1
        )
    )
    echo No previous dist was available to restore.
)
exit /b 1

:cleanup_and_fail
if exist "%STAGING_ROOT%" (
    attrib -r "%STAGING_ROOT%\*.*" /s /d >nul 2>&1
    rmdir /s /q "%STAGING_ROOT%" >nul 2>&1
)
if exist "%WORK_ROOT%" (
    attrib -r "%WORK_ROOT%\*.*" /s /d >nul 2>&1
    rmdir /s /q "%WORK_ROOT%" >nul 2>&1
)
pause
exit /b 1

:cleanup_and_exit
if exist "%STAGING_ROOT%" (
    attrib -r "%STAGING_ROOT%\*.*" /s /d >nul 2>&1
    rmdir /s /q "%STAGING_ROOT%" >nul 2>&1
)
if exist "%WORK_ROOT%" (
    attrib -r "%WORK_ROOT%\*.*" /s /d >nul 2>&1
    rmdir /s /q "%WORK_ROOT%" >nul 2>&1
)
pause
endlocal
