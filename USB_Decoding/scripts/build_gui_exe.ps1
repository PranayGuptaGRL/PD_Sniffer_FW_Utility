param(
    [switch]$InstallPyInstaller
)

$ErrorActionPreference = "Stop"

if ($InstallPyInstaller) {
    python -m pip install --upgrade pip pyinstaller
}

$workPath = Join-Path $env:TEMP ("usbpd_gui_pyinstaller_" + [Guid]::NewGuid().ToString("N"))

try {
    python -m PyInstaller --noconfirm --workpath $workPath (Join-Path $PSScriptRoot "..\usbpd_gui.spec")
}
finally {
    if (Test-Path $workPath) {
        Remove-Item -Recurse -Force $workPath
    }
}

Write-Host "Build complete. Executable: dist\usbpd_gui.exe"
Write-Host "Run: .\dist\usbpd_gui.exe"
