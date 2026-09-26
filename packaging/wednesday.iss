; Inno Setup script: turns dist\Wednesday (PyInstaller) into WednesdaySetup-<version>.exe.
; Build from the repository root on Windows:
;
;   iscc /DAppVersion=0.1.0 packaging\wednesday.iss
;
; Installs per user (no admin prompt) to %LOCALAPPDATA%\Programs\Wednesday. Settings, the
; database and logs stay in %LOCALAPPDATA%\Wednesday and survive upgrades and uninstall.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{A6AF6DF9-A869-4069-B4AF-1F9F3978D315}
AppName=Wednesday
AppVersion={#AppVersion}
AppVerName=Wednesday {#AppVersion}
AppPublisher=momentum.id
AppPublisherURL=https://github.com/mragungsetiaji/wednesday
DefaultDirName={autopf}\Wednesday
DefaultGroupName=Wednesday
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\dist
OutputBaseFilename=WednesdaySetup-{#AppVersion}
SetupIconFile=wednesday.ico
UninstallDisplayIcon={app}\Wednesday.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\Wednesday\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Dirs]
Name: "{localappdata}\Wednesday"; Flags: uninsneveruninstall

[Icons]
Name: "{group}\Wednesday"; Filename: "{app}\Wednesday.exe"
Name: "{group}\Wednesday settings folder"; Filename: "{localappdata}\Wednesday"
Name: "{group}\Uninstall Wednesday"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Wednesday"; Filename: "{app}\Wednesday.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Wednesday.exe"; Description: "{cm:LaunchProgram,Wednesday}"; Flags: nowait postinstall skipifsilent

[Code]
const
  WebView2Key = 'Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  WebView2Url = 'https://go.microsoft.com/fwlink/p/?LinkId=2124703';

{ The window is drawn by Microsoft Edge WebView2. Windows 11 ships it; some Windows 10 PCs lack it. }
function WebView2Installed: Boolean;
var
  Version: String;
begin
  Result :=
    (RegQueryStringValue(HKLM, 'SOFTWARE\WOW6432Node\' + WebView2Key, 'pv', Version) or
     RegQueryStringValue(HKLM, 'SOFTWARE\' + WebView2Key, 'pv', Version) or
     RegQueryStringValue(HKCU, 'Software\' + WebView2Key, 'pv', Version))
    and (Version <> '') and (Version <> '0.0.0.0');
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ErrorCode: Integer;
begin
  if (CurStep = ssPostInstall) and not WizardSilent and not WebView2Installed then
    if MsgBox('Wednesday needs the Microsoft Edge WebView2 Runtime, which is not installed on this PC.' + #13#10#13#10 +
              'Download it now? Run the downloaded file, then start Wednesday.',
              mbConfirmation, MB_YESNO) = IDYES then
      ShellExec('open', WebView2Url, '', '', SW_SHOWNORMAL, ewNoWait, ErrorCode);
end;
