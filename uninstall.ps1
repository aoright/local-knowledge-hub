[CmdletBinding()]
param(
    [string]$InstallDir = "",
    [switch]$PurgeData
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Assert-SafeInstallRoot([string]$Candidate) {
    $resolved = [IO.Path]::GetFullPath($Candidate).TrimEnd([IO.Path]::DirectorySeparatorChar)
    $profile = [IO.Path]::GetFullPath($env:USERPROFILE).TrimEnd([IO.Path]::DirectorySeparatorChar)
    $driveRoot = [IO.Path]::GetPathRoot($resolved).TrimEnd([IO.Path]::DirectorySeparatorChar)
    if ([string]::IsNullOrWhiteSpace($resolved) -or $resolved -eq $profile -or $resolved -eq $driveRoot) {
        throw "Unsafe install directory: $Candidate"
    }
    return $resolved
}

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if ([string]::IsNullOrWhiteSpace($InstallDir)) {
    if ((Test-Path -LiteralPath (Join-Path $scriptRoot "app")) -or (Test-Path -LiteralPath (Join-Path $scriptRoot "data"))) {
        $InstallDir = $scriptRoot
    } elseif (-not [string]::IsNullOrWhiteSpace($env:LOCAL_KNOWLEDGE_HOME)) {
        $InstallDir = $env:LOCAL_KNOWLEDGE_HOME
    } else {
        $InstallDir = Join-Path $env:LOCALAPPDATA "LocalKnowledgeHub"
    }
}
$installRoot = Assert-SafeInstallRoot $InstallDir
$venvPython = Join-Path $installRoot "venv\Scripts\python.exe"
$manager = Join-Path $installRoot "app\src\install_manager.py"
$services = Join-Path $installRoot "app\src\start_services.py"
$env:KHUB_DATA_DIR = Join-Path $installRoot "data"

if ((Test-Path -LiteralPath $venvPython -PathType Leaf) -and (Test-Path -LiteralPath $services -PathType Leaf)) {
    $stopArgs = @($services, "--stop")
    if ($PurgeData) {
        $stopArgs += "--purge"
    }
    & $venvPython @stopArgs
}
if ((Test-Path -LiteralPath $venvPython -PathType Leaf) -and (Test-Path -LiteralPath $manager -PathType Leaf)) {
    & $venvPython $manager unconfigure --install-root $installRoot
}

foreach ($name in @(
    "app",
    ".app.previous",
    "venv",
    "bin",
    "uninstall.ps1",
    "README.md",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md"
)) {
    $target = Join-Path $installRoot $name
    if (Test-Path -LiteralPath $target) {
        Remove-Item -LiteralPath $target -Recurse -Force
    }
}

if ($PurgeData) {
    $data = Join-Path $installRoot "data"
    if (Test-Path -LiteralPath $data) {
        Remove-Item -LiteralPath $data -Recurse -Force
    }
    if (Test-Path -LiteralPath $installRoot) {
        $remaining = @(Get-ChildItem -LiteralPath $installRoot -Force)
        if ($remaining.Count -eq 0) {
            Remove-Item -LiteralPath $installRoot -Force
        }
    }
    Write-Host "Removed application and data from $installRoot"
} else {
    Write-Host "Removed application; retained data at $(Join-Path $installRoot 'data')"
}
