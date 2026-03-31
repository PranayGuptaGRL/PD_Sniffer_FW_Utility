#define MyAppName "USB PD Decoder"
#define MyAppVersion "0.1.0"
#define MyAppPublisher "USB_Decoding"
#define MyAppExeName "usbpd_gui.exe"

[Setup]
AppId={{0E84A0B9-4FA1-4F1F-9DF6-D69C9DE8A9A5}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible
Compression=lzma
SolidCompression=yes
WizardStyle=modern
OutputDir=..\dist
OutputBaseFilename=usbpd_gui_setup

[Files]
Source: "..\dist\usbpd_gui.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\usb_pd_decoder\drivers\*"; DestDir: "{app}\drivers"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"

[Run]
Filename: "{sys}\pnputil.exe"; Parameters: "/add-driver ""{app}\drivers\grl_sniffer_winusb.inf"" /install"; Flags: runhidden waituntilterminated; StatusMsg: "Installing GRL sniffer WinUSB driver..."
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent
