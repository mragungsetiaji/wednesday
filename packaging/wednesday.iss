; Inno Setup script: turns dist\Wednesday (PyInstaller) into WednesdaySetup-<version>.exe.
; Build from the repository root on Windows:
;
;   iscc /DAppVersion=0.1.0 packaging\wednesday.iss
;
; Installs per user (no admin prompt) to %LOCALAPPDATA%\Programs\Wednesday. Settings, the
; database and logs stay in %LOCALAPPDATA%\Wednesday and survive upgrades and uninstall.
;
; When that folder already exists, the installer asks whether to keep it or do a fresh
; install, which deletes it and the secrets saved in Credential Manager (Wednesday.exe
; --forget-secrets). Silent installs keep the data unless run with /FRESH=1.

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
; The risk agreement must be accepted to install; the license is shown after it.
LicenseFile=..\DISCLAIMER.md
InfoBeforeFile=..\LICENSE
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\Wednesday\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion
Source: "..\DISCLAIMER.md"; DestDir: "{app}"; DestName: "DISCLAIMER.txt"; Flags: ignoreversion
Source: "..\THIRD_PARTY_NOTICES.md"; DestDir: "{app}"; DestName: "THIRD_PARTY_NOTICES.txt"; Flags: ignoreversion

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

var
  FreshPage: TInputOptionWizardPage;
  Fresh: Boolean;  { decided when installing starts; the folder exists again afterwards }

function DataDir: String;
begin
  Result := ExpandConstant('{localappdata}\Wednesday');
end;

function HasOldData: Boolean;
begin
  Result := DirExists(DataDir);
end;

procedure InitializeWizard;
begin
  if not HasOldData then
    Exit;
  FreshPage := CreateInputOptionPage(wpSelectTasks,
    'Existing data', 'Wednesday has been installed on this PC before.',
    'Its settings, database, journals, Lab models and logs are in ' + DataDir + '.' + #13#10#13#10 +
    'Keep them, or start fresh? A fresh install deletes all of them and the API keys and ' +
    'bot token saved in Windows Credential Manager. This can''t be undone.',
    True, False);
  FreshPage.Add('Keep my data and settings');
  FreshPage.Add('Fresh install: delete all previous data and settings');
  FreshPage.SelectedValueIndex := 0;
end;

function FreshInstall: Boolean;
begin
  if WizardSilent then
    Result := HasOldData and (ExpandConstant('{param:FRESH|0}') <> '0')
  else
    Result := (FreshPage <> nil) and (FreshPage.SelectedValueIndex = 1);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (FreshPage <> nil) and (CurPageID = FreshPage.ID) and (FreshPage.SelectedValueIndex = 1) then
    Result := MsgBox('Delete all Wednesday data and settings in ' + DataDir + '?' + #13#10#13#10 +
                     'Journals, Lab models and stored price history go too.',
                     mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ErrorCode: Integer;
begin
  if CurStep = ssInstall then
    Fresh := FreshInstall;
  if (CurStep = ssInstall) and Fresh then
  begin
    if not DelTree(DataDir, True, True, True) or DirExists(DataDir) then
      if not WizardSilent then
        MsgBox('Some files in ' + DataDir + ' could not be deleted, probably because Wednesday is still ' +
               'running. Close it and delete that folder by hand for a clean start.', mbError, MB_OK);
    ForceDirectories(DataDir);
  end;
  if (CurStep = ssPostInstall) and Fresh then
    Exec(ExpandConstant('{app}\Wednesday.exe'), '--forget-secrets', '', SW_HIDE, ewWaitUntilTerminated, ErrorCode);
  if (CurStep = ssPostInstall) and not WizardSilent and not WebView2Installed then
    if MsgBox('Wednesday needs the Microsoft Edge WebView2 Runtime, which is not installed on this PC.' + #13#10#13#10 +
              'Download it now? Run the downloaded file, then start Wednesday.',
              mbConfirmation, MB_YESNO) = IDYES then
      ShellExec('open', WebView2Url, '', '', SW_SHOWNORMAL, ewNoWait, ErrorCode);
end;
