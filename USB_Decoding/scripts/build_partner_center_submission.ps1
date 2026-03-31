param(
    [string]$InfPath,
    [string]$CatPath,
    [string]$PackageName = "GRLUSBPDSniffer",
    [string]$OutputDir = "",
    [string]$CabPath = "",
    [string]$DdfPath = "",
    [string]$EvCertSubject = "",
    [string]$PfxPath = "",
    [string]$PfxPassword = "",
    [string]$TimestampUrl = "",
    [switch]$SkipCabSigning
)

$ErrorActionPreference = "Stop"

function Resolve-ExistingPath {
    param(
        [string]$ExplicitPath,
        [string[]]$Candidates,
        [string]$Label
    )

    $items = @()
    if ($ExplicitPath) {
        $items += $ExplicitPath
    }
    $items += $Candidates

    foreach ($candidate in ($items | Select-Object -Unique)) {
        if ($candidate -and (Test-Path $candidate)) {
            return (Resolve-Path $candidate).Path
        }
    }

    throw "Could not locate $Label."
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

    throw "signtool.exe was not found. Install the Windows SDK on the submission machine."
}

$rootDir = Resolve-Path (Join-Path $PSScriptRoot "..")
$resolvedInfPath = Resolve-ExistingPath -ExplicitPath $InfPath -Candidates @(
    (Join-Path $rootDir "usb_pd_decoder\drivers\grl_sniffer_winusb.inf")
) -Label "driver INF"
$resolvedCatPath = Resolve-ExistingPath -ExplicitPath $CatPath -Candidates @(
    ([System.IO.Path]::ChangeExtension($resolvedInfPath, ".cat"))
) -Label "driver catalog"

if ([string]::IsNullOrWhiteSpace($OutputDir)) {
    $OutputDir = Join-Path $rootDir "dist\partner_center_submission"
}
if ([string]::IsNullOrWhiteSpace($CabPath)) {
    $CabPath = Join-Path $OutputDir "$PackageName.cab"
}
if ([string]::IsNullOrWhiteSpace($DdfPath)) {
    $DdfPath = Join-Path $OutputDir "$PackageName.ddf"
}

$packageDir = Join-Path $OutputDir $PackageName
New-Item -ItemType Directory -Path $packageDir -Force | Out-Null

$infName = Split-Path $resolvedInfPath -Leaf
$catName = Split-Path $resolvedCatPath -Leaf
$localInf = Join-Path $packageDir $infName
$localCat = Join-Path $packageDir $catName

Copy-Item $resolvedInfPath -Destination $localInf -Force
Copy-Item $resolvedCatPath -Destination $localCat -Force

$ddf = @"
.OPTION EXPLICIT
.Set CabinetFileCountThreshold=0
.Set FolderFileCountThreshold=0
.Set FolderSizeThreshold=0
.Set MaxCabinetSize=0
.Set MaxDiskFileCount=0
.Set MaxDiskSize=0
.Set CompressionType=MSZIP
.Set Cabinet=on
.Set Compress=on
.Set CabinetNameTemplate=$(Split-Path $CabPath -Leaf)
.Set DestinationDir=$PackageName
$localInf
$localCat
"@
[System.IO.File]::WriteAllText($DdfPath, $ddf, [System.Text.Encoding]::ASCII)

Push-Location $OutputDir
try {
    & makecab.exe /f $DdfPath
    if ($LASTEXITCODE -ne 0) {
        throw "makecab.exe failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}

$defaultCab = Join-Path $OutputDir "Disk1\$(Split-Path $CabPath -Leaf)"
if (-not (Test-Path $defaultCab)) {
    throw "CAB file was not created at $defaultCab"
}
Copy-Item $defaultCab -Destination $CabPath -Force

if (-not $SkipCabSigning) {
    $signtool = Find-SignTool
    $signArgs = @("sign", "/v", "/fd", "SHA256")
    if ($TimestampUrl) {
        $signArgs += @("/tr", $TimestampUrl, "/td", "SHA256")
    }
    if ($PfxPath) {
        $signArgs += @("/f", (Resolve-Path $PfxPath).Path)
        if ($PfxPassword) {
            $signArgs += @("/p", $PfxPassword)
        }
    }
    elseif ($EvCertSubject) {
        $signArgs += @("/s", "MY", "/n", $EvCertSubject)
    }
    else {
        throw "To produce a production submission CAB you must provide -EvCertSubject or -PfxPath, or pass -SkipCabSigning."
    }
    $signArgs += $CabPath
    & $signtool @signArgs
    if ($LASTEXITCODE -ne 0) {
        throw "signtool.exe failed to sign the submission CAB."
    }
}

Write-Host "Partner Center submission package created:"
Write-Host "  CAB : $CabPath"
Write-Host "  DDF : $DdfPath"
Write-Host "Upload the signed CAB to Partner Center Hardware Dashboard."
