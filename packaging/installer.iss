; Inno Setup script: wraps the PyInstaller one-folder build into a Windows installer.
;
; Build the app first (packaging\build.bat does both steps), then:
;     "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" /DAppVersion=1.1.0 packaging\installer.iss
; Output: dist\installer\DeviceAutomation-Setup-<version>.exe
;
; Installs per-user by default (no admin prompt) into %LOCALAPPDATA%\Programs, where
; the app can keep configs, logs and run history next to the .exe. Choosing "install
; for all users" puts it in Program Files; the app then keeps user data in
; %LOCALAPPDATA%\DeviceAutomation instead (see core/paths.py).

#define AppName "Device Automation Dashboard"
#define AppExe "DeviceAutomation.exe"
#define AppDataFolder "DeviceAutomation"
#define DistDir "..\dist\DeviceAutomation"
#ifndef AppVersion
  #define AppVersion "1.1.0"
#endif

[Setup]
; Keep this AppId forever: upgrades and the uninstaller find previous installs by it.
AppId={{8C3F2A51-6B7E-4D0A-9F1C-2E5D7A9B4C31}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppName}
VersionInfoVersion={#AppVersion}
VersionInfoProductName={#AppName}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist\installer
OutputBaseFilename=DeviceAutomation-Setup-{#AppVersion}
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; The program itself (replaced on every upgrade). User data folders are excluded; the leading
; backslash anchors each pattern to the build root, so _internal\default_configs is still included.
Source: "{#DistDir}\*"; DestDir: "{app}"; Excludes: "\configs\*,\logs\*,\runs\*,\settings.json"; Flags: ignoreversion recursesubdirs createallsubdirs
; Example scripts: installed once, never overwritten (so edits survive upgrades), kept on uninstall
; unless the user chooses to delete their data.
Source: "{#DistDir}\configs\*"; DestDir: "{app}\configs"; Flags: onlyifdoesntexist uninsneveruninstall recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"
Name: "{group}\{#AppName} User Guide"; Filename: "{app}\README.txt"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Created at runtime by Python/Qt; always safe to remove.
Type: filesandordirs; Name: "{app}\_internal\__pycache__"

[Code]
procedure DeleteUserData;
begin
  DelTree(ExpandConstant('{app}\configs'), True, True, True);
  DelTree(ExpandConstant('{app}\logs'), True, True, True);
  DelTree(ExpandConstant('{app}\runs'), True, True, True);
  DeleteFile(ExpandConstant('{app}\settings.json'));
  DelTree(ExpandConstant('{localappdata}\{#AppDataFolder}'), True, True, True);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    { Silent uninstalls (e.g. during an upgrade by a script) always keep the user's data. }
    if UninstallSilent then
      Exit;
    if MsgBox('Also delete your scripts, schedules, settings, logs and run history?' + #13#10 + #13#10 +
              'Choose No to keep them for a future reinstall.',
              mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
    begin
      DeleteUserData;
      RemoveDir(ExpandConstant('{app}'));
    end;
  end;
end;
