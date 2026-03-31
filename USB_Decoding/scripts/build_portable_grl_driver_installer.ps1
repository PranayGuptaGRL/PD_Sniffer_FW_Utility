param(
    [string]$InfPath,
    [string]$OutputPath = "",
    [switch]$SkipSignatureVerification
)

$ErrorActionPreference = "Stop"

function Resolve-DriverInfPath {
    param([string]$ExplicitInfPath)

    $scriptDir = Split-Path -Parent $PSCommandPath
    $candidates = @()

    if ($ExplicitInfPath) {
        $candidates += $ExplicitInfPath
    }

    $candidates += Join-Path $scriptDir "..\usb_pd_decoder\drivers\grl_sniffer_winusb.inf"
    $candidates += Join-Path $scriptDir "grl_sniffer_winusb.inf"
    $candidates += Join-Path $scriptDir "drivers\grl_sniffer_winusb.inf"

    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        if ($candidate -and (Test-Path $candidate)) {
            return (Resolve-Path $candidate).Path
        }
    }

    throw "Could not locate grl_sniffer_winusb.inf. Pass -InfPath explicitly."
}

function Find-SignTool {
    $roots = @(
        "${env:ProgramFiles(x86)}\Windows Kits\10\bin",
        "${env:ProgramFiles}\Windows Kits\10\bin"
    ) | Where-Object { $_ -and (Test-Path $_) }

    foreach ($root in $roots) {
        $match = Get-ChildItem -Path $root -Filter signtool.exe -Recurse -File -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending |
            Select-Object -First 1
        if ($match) {
            return $match.FullName
        }
    }

    $cmd = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }

    throw "signtool.exe was not found. Install the Windows SDK on the packaging machine."
}

$resolvedInfPath = Resolve-DriverInfPath -ExplicitInfPath $InfPath
$catPath = [System.IO.Path]::ChangeExtension($resolvedInfPath, ".cat")
$rootDir = Resolve-Path (Join-Path $PSScriptRoot "..")

if (-not (Test-Path $catPath)) {
    throw "Missing signed driver catalog: $catPath. Import the Microsoft-signed package from Partner Center first."
}

if (-not $SkipSignatureVerification) {
    $signtool = Find-SignTool
    & $signtool verify /v /pa $catPath
    if ($LASTEXITCODE -ne 0) {
        throw "signtool verification failed for $catPath"
    }
}

$tempDir = Join-Path $env:TEMP ("grl_driver_bundle_" + [Guid]::NewGuid().ToString("N"))
$payloadDir = Join-Path $tempDir "payload"
$zipPath = Join-Path $tempDir "payload.zip"
$infOut = Join-Path $payloadDir "grl_sniffer_winusb.inf"
$catOut = Join-Path $payloadDir "grl_sniffer_winusb.cat"

New-Item -ItemType Directory -Path $payloadDir -Force | Out-Null
Copy-Item $resolvedInfPath -Destination $infOut -Force
Copy-Item $catPath -Destination $catOut -Force

Compress-Archive -Path (Join-Path $payloadDir "*") -DestinationPath $zipPath -Force
$payloadBase64 = [Convert]::ToBase64String([System.IO.File]::ReadAllBytes($zipPath))
$payloadLines = ($payloadBase64 -split "(.{1,120})" | Where-Object { $_ })

if ([string]::IsNullOrWhiteSpace($OutputPath)) {
    $OutputPath = Join-Path $rootDir "dist\install_grl_winusb_driver_portable.cmd"
}

$portableScript = @'
@echo off
setlocal
net session >nul 2>&1
if %errorlevel% neq 0 (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
 "$ErrorActionPreference='Stop';" ^
 "$self='%~f0';" ^
 "$lines=Get-Content -LiteralPath $self;" ^
 "$marker='__PAYLOAD_BELOW__';" ^
 "$idx=[Array]::IndexOf($lines,$marker);" ^
 "if($idx -lt 0){throw 'Payload marker not found.'};" ^
 "$payload=[string]::Join('', $lines[($idx + 1)..($lines.Length - 1)]);" ^
 "$temp=Join-Path $env:TEMP ('grl_driver_install_' + [Guid]::NewGuid().ToString('N'));" ^
 "New-Item -ItemType Directory -Path $temp -Force | Out-Null;" ^
 "$zip=Join-Path $temp 'payload.zip';" ^
 "[System.IO.File]::WriteAllBytes($zip,[Convert]::FromBase64String($payload));" ^
 "Expand-Archive -LiteralPath $zip -DestinationPath $temp -Force;" ^
 "$inf=Join-Path $temp 'grl_sniffer_winusb.inf';" ^
 "& (Join-Path $env:WINDIR 'System32\pnputil.exe') /add-driver $inf /install;" ^
 "if($LASTEXITCODE -ne 0){throw ('pnputil failed with exit code ' + $LASTEXITCODE)};" ^
 "Remove-Item -LiteralPath $temp -Recurse -Force;" ^
 "Write-Host 'GRL WinUSB production driver installed successfully. Replug the sniffer if it was already connected.'"

if errorlevel 1 (
  echo Driver install failed.
  pause
  exit /b %errorlevel%
)

echo Driver install finished successfully.
pause
exit /b 0
__PAYLOAD_BELOW__
'@

$content = $portableScript + "`r`n" + ($payloadLines -join "`r`n") + "`r`n"
[System.IO.File]::WriteAllText($OutputPath, $content, [System.Text.Encoding]::ASCII)

Remove-Item -LiteralPath $tempDir -Recurse -Force

Write-Host "Portable single-file driver installer created:"
Write-Host "  $OutputPath"
