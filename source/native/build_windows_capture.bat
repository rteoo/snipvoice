@echo off
setlocal
set "SNIPVOICE_CAPTURE_DEFINES="
set "SNIPVOICE_CAPTURE_TARGET=snipvoice-capture.exe"
if "%~1"=="--fault-tests" (
    set "SNIPVOICE_CAPTURE_DEFINES=/DSNIPVOICE_CAPTURE_FAULT_TESTS"
    set "SNIPVOICE_CAPTURE_TARGET=snipvoice-capture-fault-test.exe"
) else if not "%~1"=="" (
    echo Unknown native build option. 1>&2
    exit /b 1
)
rem Compile with the installed MSVC toolchain and Windows SDK; never install tools.
where cl.exe >nul 2>nul
if errorlevel 1 (
    if not exist "%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe" goto missing
    for /f "usebackq tokens=*" %%I in (`"%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath`) do call "%%I\VC\Auxiliary\Build\vcvars64.bat" >nul
    where cl.exe >nul 2>nul
    if errorlevel 1 goto missing
)
if not exist "%~dp0bin" mkdir "%~dp0bin"
if not exist "%~dp0bin" goto directory
pushd "%~dp0bin"
if errorlevel 1 goto directory
cl.exe /nologo /std:c++17 /EHsc /W4 /O2 /MT /DUNICODE /D_UNICODE %SNIPVOICE_CAPTURE_DEFINES% "%~dp0windows_capture.cpp" /Fe:%SNIPVOICE_CAPTURE_TARGET% /Fo:windows_capture.obj /link ole32.lib propsys.lib uuid.lib /SUBSYSTEM:CONSOLE
set "SNIPVOICE_BUILD_RESULT=%errorlevel%"
popd
exit /b %SNIPVOICE_BUILD_RESULT%
:directory
echo Cannot create or access the native helper output directory. Check workspace permissions. 1>&2
exit /b 1
:missing
echo MSVC C++ tools and Windows SDK are required. Run from an x64 Native Tools prompt or provision the C++ build tools explicitly. 1>&2
exit /b 1
