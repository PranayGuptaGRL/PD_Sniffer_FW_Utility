#define MyDriverName "GRL WinUSB Driver"
#define MyDriverVersion "0.1.0"
#define MyDriverPublisher "USB_Decoding"

[Setup]
AppId={{E439C0E9-5C7D-45C1-9E84-AC3A46C4157E}
AppName={#MyDriverName}
AppVersion={#MyDriverVersion}
AppPublisher={#MyDriverPublisher}
DefaultDirName={autopf}\{#MyDriverName}
DefaultGroupName={#MyDriverName}
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible
Compression=lzma
SolidCompression=yes
WizardStyle=modern
OutputDir=..\dist
OutputBaseFilename=grl_winusb_driver_setup

[Files]
Source: "..\usb_pd_decoder\drivers\*"; DestDir: "{app}\drivers"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\scripts\install_grl_winusb_driver.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\scripts\grl_winusb_driver_README.txt"; DestDir: "{app}"; Flags: ignoreversion

[Run]
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\install_grl_winusb_driver.ps1"" -InfPath ""{app}\drivers\grl_sniffer_winusb.inf"" -NoElevate"; Flags: runhidden waituntilterminated; StatusMsg: "Installing GRL sniffer WinUSB driver..."
