; Inno Setup script for Snipvoice — per-user install, no admin required.
;
; Build: run build_release.bat first (produces dist\Snipvoice), then
; build_installer.bat (compiles this script into installer\Output\).
;
; User data lives in %USERPROFILE%\.snipvoice and is intentionally NOT removed
; on uninstall — the installer only manages the program files under {app}.
; Snipvoice never migrates another application's user data.

#define MyAppName "Snipvoice"
#define MyAppVersion "3.3.2"
#define MyAppChannel "stable"
#if MyAppChannel == "beta"
  #define MyAppDisplayVersion MyAppVersion + " beta"
  #define MyInstallerVersion MyAppVersion + "-beta"
#else
  #define MyAppDisplayVersion MyAppVersion
  #define MyInstallerVersion MyAppVersion
#endif
#define MyAppPublisher "Project Contributors"
#define MyAppExeName "Snipvoice.exe"
#define MyAppIcon "..\source\snipvoice.ico"
#define MyDistDir "..\dist\Snipvoice"

[Setup]
; Stable AppId so future versions upgrade in place instead of installing twice.
AppId={{61A78E44-1727-4D41-80AF-60D07AA49BC1}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppDisplayVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; Per-user install: no administrator/UAC prompt.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=Output
OutputBaseFilename=SnipvoiceSetup-{#MyInstallerVersion}
Compression=lzma2
SolidCompression=yes
SetupIconFile={#MyAppIcon}
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName} {#MyAppDisplayVersion}
WizardStyle=modern
; Snipvoice's mutex is independent of Sniptype and Txt Xpander.
AppMutex=SnipvoiceSingleton

[Languages]
Name: "brazilianportuguese"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; Flags: unchecked
Name: "startup"; Description: "Iniciar o {#MyAppName} automaticamente com o Windows"; GroupDescription: "Inicialização:"; Flags: unchecked

[Files]
Source: "{#MyDistDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Desinstalar {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
Name: "{userstartup}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: startup

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent
