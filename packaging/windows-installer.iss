; Windows installer for the desktop app (Inno Setup 6). Built by
;   python scripts/build_app.py --desktop
; after "dist\Open Transfer.exe" exists; CI installs, starts and uninstalls it.
;
; Per user, no admin prompt: installs to %LOCALAPPDATA%\Programs\Open Transfer,
; adds Start menu (and optionally desktop) shortcuts and an entry under
; Settings > Apps. Uninstalling keeps received files, pairings and history:
; they live in Downloads\Open Transfer, not here.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
; Never change AppId: it's how an update finds the installed copy.
AppId={{FDFA410C-A35E-43AD-AC5C-CEEBBD85878C}
AppName=Open Transfer
AppVersion={#AppVersion}
AppPublisher=itsonu
AppPublisherURL=https://github.com/itsonu/open-transfer
DefaultDirName={localappdata}\Programs\Open Transfer
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=open-transfer-windows-x64-setup
SetupIconFile=app_icon.ico
UninstallDisplayIcon={app}\Open Transfer.exe
UninstallDisplayName=Open Transfer
WizardStyle=modern
Compression=lzma2
SolidCompression=yes
; An open window is closed (and reopened after an update) instead of failing.
CloseApplications=yes
RestartApplications=yes

[Tasks]
Name: desktopicon; Description: "{cm:CreateDesktopIcon}"; Flags: unchecked

[Files]
Source: "..\dist\Open Transfer.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\Open Transfer"; Filename: "{app}\Open Transfer.exe"
Name: "{autodesktop}\Open Transfer"; Filename: "{app}\Open Transfer.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Open Transfer.exe"; Description: "{cm:LaunchProgram,Open Transfer}"; Flags: nowait postinstall skipifsilent

[Code]
// "Start at login" is the app's own Run entry; it would point at a deleted .exe.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Run', 'Open Transfer');
end;
