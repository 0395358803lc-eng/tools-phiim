#define MyAppName "TH Media"
#define MyAppVersion "1.8.3"
#define MyAppPublisher "TH Media"
#define MyAppExeName "THMedia.exe"
#define WebView2Bootstrapper "MicrosoftEdgeWebview2Setup.exe"
#define WebView2ClientGuid "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"

[Setup]
AppId={{D7E4D612-7C4F-4AC4-A61B-48E1C6C45831}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\TH Media
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\dist\installer
OutputBaseFilename=THMedia-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
UninstallDisplayIcon={app}\{#MyAppExeName}

[Files]
Source: "..\dist\THMedia.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\THIRD_PARTY_LICENSES.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\sbom.cdx.json"; DestDir: "{app}"; Flags: ignoreversion
Source: "prereqs\{#WebView2Bootstrapper}"; Flags: dontcopy

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[InstallDelete]
Type: files; Name: "{app}\FlowStoryStudio.exe"
Type: files; Name: "{autoprograms}\Flow Story Studio.lnk"
Type: files; Name: "{autodesktop}\Flow Story Studio.lnk"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Code]
function IsValidWebView2Version(const Version: String): Boolean;
begin
  Result := (Version <> '') and (Version <> '0.0.0.0');
end;

function WebView2RuntimeInstalled(): Boolean;
var
  Version: String;
  ClientKey: String;
begin
  Result := False;
  ClientKey := 'Software\Microsoft\EdgeUpdate\Clients\{#WebView2ClientGuid}';

  if IsWin64 then
  begin
    if RegQueryStringValue(HKLM32, ClientKey, 'pv', Version) and
       IsValidWebView2Version(Version) then
    begin
      Result := True;
      exit;
    end;
  end
  else
  begin
    if RegQueryStringValue(HKLM, ClientKey, 'pv', Version) and
       IsValidWebView2Version(Version) then
    begin
      Result := True;
      exit;
    end;
  end;

  if RegQueryStringValue(HKCU, ClientKey, 'pv', Version) and
     IsValidWebView2Version(Version) then
  begin
    Result := True;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
  BootstrapperPath: String;
begin
  Result := '';

  if WebView2RuntimeInstalled() then
    exit;

  WizardForm.StatusLabel.Caption := 'Đang cài Microsoft Edge WebView2 Runtime...';
  ExtractTemporaryFile('{#WebView2Bootstrapper}');
  BootstrapperPath := ExpandConstant('{tmp}\{#WebView2Bootstrapper}');

  if not Exec(
    BootstrapperPath,
    '/silent /install',
    '',
    SW_HIDE,
    ewWaitUntilTerminated,
    ResultCode
  ) then
  begin
    Result := 'Unable to start Microsoft Edge WebView2 Runtime installer.';
    exit;
  end;

  if not WebView2RuntimeInstalled() then
  begin
    Result := 'Unable to install Microsoft Edge WebView2 Runtime (exit code ' +
      IntToStr(ResultCode) + '). Check the Internet connection or Windows policy, then run Setup again.';
    exit;
  end;

  WizardForm.StatusLabel.Caption := 'Microsoft Edge WebView2 Runtime is ready.';
end;
