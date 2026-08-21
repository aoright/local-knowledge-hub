#ifndef MyAppVersion
  #define MyAppVersion "1.3.1"
#endif

[Setup]
AppId={{6EA9AF90-8A1D-48F0-A120-F6DAB8716F31}
AppName=Local Knowledge Hub
AppVersion={#MyAppVersion}
AppPublisher=aoright
AppPublisherURL=https://github.com/aoright/local-knowledge-hub
AppSupportURL=https://github.com/aoright/local-knowledge-hub/issues
AppUpdatesURL=https://github.com/aoright/local-knowledge-hub/releases
DefaultDirName={localappdata}\LocalKnowledgeHub
CreateAppDir=no
DisableProgramGroupPage=yes
LicenseFile=LICENSE
OutputDir=dist
OutputBaseFilename=LocalKnowledgeHub-Setup-{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
WizardStyle=modern
Uninstallable=no
SetupLogging=yes
CloseApplications=no

[Types]
Name: "full"; Description: "Complete installation (recommended)"
Name: "compact"; Description: "Core local knowledge only"

[Components]
Name: "core"; Description: "Local knowledge, memory, indexing, and client integration"; Types: full compact; Flags: fixed
Name: "services"; Description: "Onyx UI and private SearXNG web search (requires Docker Desktop)"; Types: full

[Tasks]
Name: "autoupdate"; Description: "Enable automatic updates (recommended)"; Flags: checkedonce

[Files]
Source: "app\*"; DestDir: "{tmp}\LocalKnowledgeHubPackage\app"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "install.ps1"; DestDir: "{tmp}\LocalKnowledgeHubPackage"; Flags: ignoreversion
Source: "uninstall.ps1"; DestDir: "{tmp}\LocalKnowledgeHubPackage"; Flags: ignoreversion
Source: "README.md"; DestDir: "{tmp}\LocalKnowledgeHubPackage"; Flags: ignoreversion
Source: "LICENSE"; DestDir: "{tmp}\LocalKnowledgeHubPackage"; Flags: ignoreversion
Source: "THIRD_PARTY_NOTICES.md"; DestDir: "{tmp}\LocalKnowledgeHubPackage"; Flags: ignoreversion
Source: "VERSION"; DestDir: "{tmp}\LocalKnowledgeHubPackage"; Flags: ignoreversion; AfterInstall: RunInstaller

[Code]
procedure RunInstaller;
var
  PowerShellPath: String;
  InstallerPath: String;
  Parameters: String;
  ResultCode: Integer;
begin
  PowerShellPath := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
  InstallerPath := ExpandConstant('{tmp}\LocalKnowledgeHubPackage\install.ps1');
  Parameters := '-NoProfile -ExecutionPolicy Bypass -File "' + InstallerPath + '"';
  if not WizardIsComponentSelected('services') then
    Parameters := Parameters + ' -WithoutServices';
  if WizardIsTaskSelected('autoupdate') then
    Parameters := Parameters + ' -EnableAutoUpdate'
  else
    Parameters := Parameters + ' -NoAutoUpdate';

  WizardForm.StatusLabel.Caption := 'Configuring Local Knowledge Hub and installing dependencies...';
  if not Exec(PowerShellPath, Parameters, '', SW_SHOW, ewWaitUntilTerminated, ResultCode) then
    RaiseException('Could not start PowerShell installer.');
  if ResultCode <> 0 then
    RaiseException(Format('Local Knowledge Hub installation failed with exit code %d. Review the PowerShell output for details.', [ResultCode]));
end;
