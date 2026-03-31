param(
    [switch]$InstallPyInstaller
)

$ErrorActionPreference = "Stop"

if ($InstallPyInstaller) {
    python -m pip install --upgrade pip pyinstaller
}

$workPath = Join-Path $env:TEMP ("usbpd_txt_decoder_pyinstaller_" + [Guid]::NewGuid().ToString("N"))

try {
    python -m PyInstaller --noconfirm --workpath $workPath (Join-Path $PSScriptRoot "..\usbpd_txt_decoder.spec")
}
finally {
    if (Test-Path $workPath) {
        Remove-Item -Recurse -Force $workPath
    }
}

Write-Host "Build complete. Executable: dist\\usbpd_txt_decoder.exe"
Write-Host "Example: .\\dist\\usbpd_txt_decoder.exe --input samples\\usblyzer_twinkie_sample.txt --json --print"
