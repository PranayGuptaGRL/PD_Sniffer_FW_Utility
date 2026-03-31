param(
    [string]$OutputDir = ""
)

$ErrorActionPreference = "Stop"

$rootDir = Resolve-Path (Join-Path $PSScriptRoot "..")
$driverInf = Join-Path $rootDir "usb_pd_decoder\drivers\grl_sniffer_winusb.inf"
$driverCat = Join-Path $rootDir "usb_pd_decoder\drivers\grl_sniffer_winusb.cat"
$driverReadme = Join-Path $rootDir "usb_pd_decoder\drivers\README.txt"
$standaloneReadme = Join-Path $rootDir "scripts\grl_winusb_driver_README.txt"
$installScript = Join-Path $rootDir "scripts\install_grl_winusb_driver.ps1"

if (-not (Test-Path $driverInf)) {
    throw "Missing driver INF: $driverInf"
}

if (-not (Test-Path $driverCat)) {
    throw "Missing signed driver catalog: $driverCat. Import the Microsoft-signed package from Partner Center first, then build the standalone driver package."
}

if ([string]::IsNullOrWhiteSpace($OutputDir)) {
    $OutputDir = Join-Path $rootDir "dist\grl_winusb_driver"
}

New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null

Copy-Item $driverInf -Destination $OutputDir -Force
Copy-Item $driverCat -Destination $OutputDir -Force
Copy-Item $driverReadme -Destination $OutputDir -Force
Copy-Item $standaloneReadme -Destination $OutputDir -Force
Copy-Item $installScript -Destination $OutputDir -Force

$zipPath = "$OutputDir.zip"
Compress-Archive -Path (Join-Path $OutputDir "*") -DestinationPath $zipPath -Force

Write-Host "Standalone driver package created:"
Write-Host "  Folder: $OutputDir"
Write-Host "  Zip   : $zipPath"
