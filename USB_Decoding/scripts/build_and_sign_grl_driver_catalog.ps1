param(
    [string]$InfPath,
    [string]$OsList = "10_X64,10_VB_X64,10_NI_X64,10_GE_X64",
    [string]$CertThumbprint,
    [string]$PfxPath,
    [string]$PfxPassword,
    [string]$CertSubject = "CN=GRL USB PD Sniffer Test Signing",
    [string]$TimestampUrl = "",
    [switch]$CreateTestCertificate,
    [switch]$SkipVerification,
    [switch]$NoElevate
)

$ErrorActionPreference = "Stop"

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Invoke-SelfElevated {
    $args = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", "`"$PSCommandPath`""
    )

    if ($InfPath) { $args += @("-InfPath", "`"$InfPath`"") }
    if ($OsList) { $args += @("-OsList", "`"$OsList`"") }
    if ($CertThumbprint) { $args += @("-CertThumbprint", "`"$CertThumbprint`"") }
    if ($PfxPath) { $args += @("-PfxPath", "`"$PfxPath`"") }
    if ($PfxPassword) { $args += @("-PfxPassword", "`"$PfxPassword`"") }
    if ($CertSubject) { $args += @("-CertSubject", "`"$CertSubject`"") }
    if ($TimestampUrl) { $args += @("-TimestampUrl", "`"$TimestampUrl`"") }
    if ($CreateTestCertificate) { $args += "-CreateTestCertificate" }
    if ($SkipVerification) { $args += "-SkipVerification" }
    $args += "-NoElevate"

    $proc = Start-Process -FilePath "powershell.exe" -Verb RunAs -Wait -PassThru -ArgumentList $args
    if ($null -eq $proc) {
        throw "UAC elevation was cancelled."
    }

    exit $proc.ExitCode
}

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

function Find-WindowsKitTool {
    param([string]$ToolName)

    $roots = @(
        "${env:ProgramFiles(x86)}\Windows Kits\10\bin",
        "${env:ProgramFiles}\Windows Kits\10\bin"
    ) | Where-Object { $_ -and (Test-Path $_) }

    foreach ($root in $roots) {
        $match = Get-ChildItem -Path $root -Filter $ToolName -Recurse -File -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending |
            Select-Object -First 1
        if ($match) {
            return $match.FullName
        }
    }

    $cmd = Get-Command $ToolName -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }

    throw "$ToolName was not found. Install the Windows SDK/WDK tools first."
}

function Invoke-ExternalTool {
    param(
        [string]$FilePath,
        [string[]]$Arguments
    )

    Write-Host "Running: $FilePath $($Arguments -join ' ')"
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$([System.IO.Path]::GetFileName($FilePath)) failed with exit code $LASTEXITCODE"
    }
}

function Ensure-TestCertificate {
    param([string]$Subject)

    $existing = Get-ChildItem Cert:\LocalMachine\My |
        Where-Object { $_.Subject -eq $Subject } |
        Sort-Object NotAfter -Descending |
        Select-Object -First 1

    if (-not $existing) {
        $existing = New-SelfSignedCertificate `
            -Type CodeSigningCert `
            -Subject $Subject `
            -CertStoreLocation Cert:\LocalMachine\My `
            -KeyAlgorithm RSA `
            -KeyLength 2048 `
            -HashAlgorithm SHA256 `
            -KeyExportPolicy Exportable `
            -NotAfter (Get-Date).AddYears(3)
    }

    $rootStore = New-Object System.Security.Cryptography.X509Certificates.X509Store("Root", "LocalMachine")
    $publisherStore = New-Object System.Security.Cryptography.X509Certificates.X509Store("TrustedPublisher", "LocalMachine")
    $rootStore.Open("ReadWrite")
    $publisherStore.Open("ReadWrite")

    try {
        if (-not ($rootStore.Certificates | Where-Object Thumbprint -eq $existing.Thumbprint)) {
            $rootStore.Add($existing)
        }
        if (-not ($publisherStore.Certificates | Where-Object Thumbprint -eq $existing.Thumbprint)) {
            $publisherStore.Add($existing)
        }
    }
    finally {
        $rootStore.Close()
        $publisherStore.Close()
    }

    return $existing.Thumbprint
}

$resolvedInfPath = Resolve-DriverInfPath -ExplicitInfPath $InfPath
$driverDir = Split-Path -Parent $resolvedInfPath
$catPath = [System.IO.Path]::ChangeExtension($resolvedInfPath, ".cat")

if (-not (Test-IsAdministrator)) {
    if ($NoElevate) {
        throw "Administrator rights are required to build or sign the GRL driver catalog."
    }
    Invoke-SelfElevated
}

$inf2cat = Find-WindowsKitTool -ToolName "Inf2Cat.exe"
Invoke-ExternalTool -FilePath $inf2cat -Arguments @(
    "/driver:$driverDir",
    "/os:$OsList",
    "/uselocaltime",
    "/verbose"
)

if (-not (Test-Path $catPath)) {
    throw "Catalog file was not generated: $catPath"
}

if ($CreateTestCertificate) {
    $CertThumbprint = Ensure-TestCertificate -Subject $CertSubject
    Write-Host "Using test certificate thumbprint: $CertThumbprint"
}

if (-not $CertThumbprint -and -not $PfxPath) {
    Write-Host "Catalog created: $catPath"
    Write-Host "No signing certificate was provided. The catalog is unsigned."
    exit 0
}

$signtool = Find-WindowsKitTool -ToolName "signtool.exe"
$signArgs = @("sign", "/v", "/fd", "SHA256")

if ($TimestampUrl) {
    $signArgs += @("/tr", $TimestampUrl, "/td", "SHA256")
}

if ($PfxPath) {
    $resolvedPfxPath = (Resolve-Path $PfxPath).Path
    $signArgs += @("/f", $resolvedPfxPath)
    if ($PfxPassword) {
        $signArgs += @("/p", $PfxPassword)
    }
}
else {
    $signArgs += @("/sha1", $CertThumbprint, "/s", "My", "/sm")
}

$signArgs += $catPath
Invoke-ExternalTool -FilePath $signtool -Arguments $signArgs

if (-not $SkipVerification) {
    Invoke-ExternalTool -FilePath $signtool -Arguments @("verify", "/v", "/pa", $catPath)
}

Write-Host "Catalog created and signed: $catPath"
