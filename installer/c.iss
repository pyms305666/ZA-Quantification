; ZA量化-C直连版 安装向导脚本（Inno Setup 6.5+）
; 版本号 V1.1.1（与 Release 一致）；内部构建号 v1.2.2-c-diff

#define MyAppName "ZA量化-C直连版"
#define MyAppVersion "1.1.1"
#define MyAppExeName "ZA量化-C直连版.exe"

[Setup]
AppId={{374EB771-E1A3-4474-909C-253B40801FD2}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} V1.1.1
DefaultDirName={autopf}\{#MyAppName}
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=ZAQuant-V1.1.1-C-Direct-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\assets\icon.ico
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
ArchitecturesInstallIn64BitMode=x64compatible
ShowLanguageDialog=no

[Languages]
Name: "chs"; MessagesFile: "ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式(&D)"; GroupDescription: "附加任务："; Flags: unchecked

[Files]
Source: "..\dist\ZA量化-C直连版.exe"; DestDir: "{app}"; DestName: "{#MyAppExeName}"; Flags: ignoreversion
Source: "使用说明-C.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "立即运行 {#MyAppName}"; Flags: nowait postinstall skipifsilent
