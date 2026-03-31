param(
    [switch]$InstallPyInstaller
)

$ErrorActionPreference = "Stop"

if ($InstallPyInstaller) {
    python -m pip install --upgrade pip pyinstaller
}

$driverInf = Join-Path $PSScriptRoot "..\usb_pd_decoder\drivers\grl_sniffer_winusb.inf"
$driverCat = Join-Path $PSScriptRoot "..\usb_pd_decoder\drivers\grl_sniffer_winusb.cat"
$specPath = Join-Path $PSScriptRoot "..\usbpd_gui.spec"
$issPath = Join-Path $PSScriptRoot "..\installer\usbpd_gui.iss"

if (-not (Test-Path $driverInf)) {
    throw "Missing driver INF: $driverInf"
}

if (-not (Test-Path $driverCat)) {
    throw "Missing signed driver catalog: $driverCat. Create and sign the catalog before building the installer."
}

$iscc = Get-Command iscc.exe -ErrorAction SilentlyContinue
if (-not $iscc) {
    throw "Inno Setup compiler (iscc.exe) was not found on PATH."
}

python -m PyInstaller --noconfirm $specPath
& $iscc.Source $issPath

Write-Host "Installer build complete. Output: dist\usbpd_gui_setup.exe"
