#define MyAppName "RapidGet"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "Stonepit Labs"
#define MyAppExeName "RapidGet.exe"

[Setup]
AppId={{7B35D3DF-BC58-4F86-90B5-0C709C5E5140}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=C:\Users\TANJIR\Desktop
OutputBaseFilename=RapidGet_Setup
SetupIconFile=C:\Users\TANJIR\Desktop\files\rapidget.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64
UninstallDisplayIcon={app}\{#MyAppExeName}
PrivilegesRequired=admin

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: checkedonce

[Files]
Source: "C:\Users\TANJIR\Desktop\files\dist\RapidGet.exe"; DestDir: "{app}"; DestName: "{#MyAppExeName}"; Flags: ignoreversion
Source: "C:\Users\TANJIR\Desktop\files\rapidget.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "C:\Users\TANJIR\Desktop\files\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "C:\Users\TANJIR\Desktop\files\Install_Browser_Extension.ps1"; DestDir: "{app}\browser_extension"; Flags: ignoreversion
Source: "C:\Users\TANJIR\Desktop\files\manifest.json"; DestDir: "{app}\browser_extension"; Flags: ignoreversion
Source: "C:\Users\TANJIR\Desktop\files\background.js"; DestDir: "{app}\browser_extension"; Flags: ignoreversion
Source: "C:\Users\TANJIR\Desktop\files\content.js"; DestDir: "{app}\browser_extension"; Flags: ignoreversion
Source: "C:\Users\TANJIR\Desktop\files\popup.html"; DestDir: "{app}\browser_extension"; Flags: ignoreversion
Source: "C:\Users\TANJIR\Desktop\files\popup.js"; DestDir: "{app}\browser_extension"; Flags: ignoreversion
Source: "C:\Users\TANJIR\Desktop\files\icon16.png"; DestDir: "{app}\browser_extension"; Flags: ignoreversion
Source: "C:\Users\TANJIR\Desktop\files\icon32.png"; DestDir: "{app}\browser_extension"; Flags: ignoreversion
Source: "C:\Users\TANJIR\Desktop\files\icon48.png"; DestDir: "{app}\browser_extension"; Flags: ignoreversion
Source: "C:\Users\TANJIR\Desktop\files\icon128.png"; DestDir: "{app}\browser_extension"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{commondesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent
