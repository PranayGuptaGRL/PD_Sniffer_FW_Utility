param(
    [string]$InfPath,
    [switch]$NoElevate
)

$ErrorActionPreference = "Stop"

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Resolve-DriverInfPath {
    param([string]$ExplicitInfPath)

    $scriptDir = Split-Path -Parent $PSCommandPath
    $candidates = @()

    if ($ExplicitInfPath) {
        $candidates += $ExplicitInfPath
    }

    $candidates += Join-Path $scriptDir "grl_sniffer_winusb.inf"
    $candidates += Join-Path $scriptDir "drivers\grl_sniffer_winusb.inf"
    $candidates += Join-Path $scriptDir "..\usb_pd_decoder\drivers\grl_sniffer_winusb.inf"

    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        if ($candidate -and (Test-Path $candidate)) {
            return (Resolve-Path $candidate).Path
        }
    }

    throw "Could not locate grl_sniffer_winusb.inf. Pass -InfPath or place the script next to the driver files."
}

function Invoke-SelfElevated {
    param([string]$ResolvedInfPath)

    $args = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", "`"$PSCommandPath`"",
        "-InfPath", "`"$ResolvedInfPath`"",
        "-NoElevate"
    )

    $proc = Start-Process -FilePath "powershell.exe" -Verb RunAs -Wait -PassThru -ArgumentList $args
    if ($null -eq $proc) {
        throw "UAC elevation was cancelled."
    }

    exit $proc.ExitCode
}

if ($env:OS -ne "Windows_NT") {
    throw "This script can only run on Windows."
}

$resolvedInfPath = Resolve-DriverInfPath -ExplicitInfPath $InfPath
$resolvedCatPath = [System.IO.Path]::ChangeExtension($resolvedInfPath, ".cat")

if (-not (Test-Path $resolvedCatPath)) {
    throw "Missing signed driver catalog: $resolvedCatPath"
}

if (-not (Test-IsAdministrator)) {
    if ($NoElevate) {
        throw "Administrator rights are required to install the GRL WinUSB driver."
    }
    Invoke-SelfElevated -ResolvedInfPath $resolvedInfPath
}

$pnputil = Join-Path $env:WINDIR "System32\pnputil.exe"
if (-not (Test-Path $pnputil)) {
    throw "pnputil.exe was not found at $pnputil"
}

Write-Host "Installing GRL WinUSB driver from $resolvedInfPath"
& $pnputil /add-driver $resolvedInfPath /install

if ($LASTEXITCODE -ne 0) {
    throw "pnputil failed with exit code $LASTEXITCODE"
}

Write-Host "Driver install completed. Replug the sniffer if it was already connected."
