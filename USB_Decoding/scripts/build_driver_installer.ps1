param()

$ErrorActionPreference = "Stop"

$driverInf = Join-Path $PSScriptRoot "..\usb_pd_decoder\drivers\grl_sniffer_winusb.inf"
$driverCat = Join-Path $PSScriptRoot "..\usb_pd_decoder\drivers\grl_sniffer_winusb.cat"
$issPath = Join-Path $PSScriptRoot "..\installer\grl_winusb_driver.iss"

if (-not (Test-Path $driverInf)) {
    throw "Missing driver INF: $driverInf"
}

if (-not (Test-Path $driverCat)) {
    throw "Missing signed driver catalog: $driverCat. Import the Microsoft-signed package from Partner Center first, then build the standalone driver installer."
}

$iscc = Get-Command iscc.exe -ErrorAction SilentlyContinue
if (-not $iscc) {
    throw "Inno Setup compiler (iscc.exe) was not found on PATH."
}

& $iscc.Source $issPath

Write-Host "Standalone driver installer build complete. Output: dist\grl_winusb_driver_setup.exe"
