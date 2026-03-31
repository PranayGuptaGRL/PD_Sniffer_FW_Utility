param(
    [string]$SourceDir,
    [string]$InfPath,
    [string]$CatPath,
    [switch]$SkipSignatureVerification
)

$ErrorActionPreference = "Stop"

function Resolve-ImportedFile {
    param(
        [string]$ExplicitPath,
        [string]$DirPath,
        [string]$Filter
    )

    if ($ExplicitPath) {
        if (-not (Test-Path $ExplicitPath)) {
            throw "Missing file: $ExplicitPath"
        }
        return (Resolve-Path $ExplicitPath).Path
    }

    if (-not $DirPath) {
        throw "Pass -SourceDir or the explicit file path."
    }

    $match = Get-ChildItem -Path $DirPath -Recurse -Filter $Filter -File |
        Sort-Object FullName |
        Select-Object -First 1

    if (-not $match) {
        throw "Could not locate $Filter under $DirPath"
    }

    return $match.FullName
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

$rootDir = Resolve-Path (Join-Path $PSScriptRoot "..")
$driverRepoDir = Join-Path $rootDir "usb_pd_decoder\drivers"

$resolvedInf = Resolve-ImportedFile -ExplicitPath $InfPath -DirPath $SourceDir -Filter "grl_sniffer_winusb.inf"
$resolvedCat = Resolve-ImportedFile -ExplicitPath $CatPath -DirPath $SourceDir -Filter "grl_sniffer_winusb.cat"

if (-not $SkipSignatureVerification) {
    $signtool = Find-SignTool
    & $signtool verify /v /pa $resolvedCat
    if ($LASTEXITCODE -ne 0) {
        throw "signtool verification failed for $resolvedCat"
    }
}

Copy-Item $resolvedInf -Destination (Join-Path $driverRepoDir "grl_sniffer_winusb.inf") -Force
Copy-Item $resolvedCat -Destination (Join-Path $driverRepoDir "grl_sniffer_winusb.cat") -Force

Write-Host "Imported signed driver package into repo:"
Write-Host "  INF : $(Join-Path $driverRepoDir 'grl_sniffer_winusb.inf')"
Write-Host "  CAT : $(Join-Path $driverRepoDir 'grl_sniffer_winusb.cat')"
