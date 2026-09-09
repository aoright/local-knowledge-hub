[CmdletBinding()]
param(
    [string]$InstallDir = "",
    [switch]$WithoutServices,
    [switch]$NoStart,
    [switch]$SkipPythonDeps,
    [switch]$EnableAutoUpdate,
    [switch]$NoAutoUpdate,
    [switch]$WithAntigravityIDE
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Test-IsWindows {
    return [Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT
}

function Assert-SafeInstallRoot([string]$Candidate) {
    $resolved = [IO.Path]::GetFullPath($Candidate).TrimEnd([IO.Path]::DirectorySeparatorChar)
    $profile = [IO.Path]::GetFullPath($env:USERPROFILE).TrimEnd([IO.Path]::DirectorySeparatorChar)
    $driveRoot = [IO.Path]::GetPathRoot($resolved).TrimEnd([IO.Path]::DirectorySeparatorChar)
    if ([string]::IsNullOrWhiteSpace($resolved) -or $resolved -eq $profile -or $resolved -eq $driveRoot) {
        throw "Unsafe install directory: $Candidate"
    }
    return $resolved
}

function Get-PythonLauncher {
    $candidates = @(
        @{ Command = "py.exe"; Prefix = @("-3") },
        @{ Command = "python.exe"; Prefix = @() },
        @{ Command = "python3.exe"; Prefix = @() }
    )
    foreach ($candidate in $candidates) {
        $resolved = Get-Command $candidate.Command -ErrorAction SilentlyContinue
        if ($null -eq $resolved) {
            continue
        }
        $checkArgs = @($candidate.Prefix) + @(
            "-c",
            "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
        )
        & $resolved.Source @checkArgs | Out-Null
        if ($LASTEXITCODE -eq 0) {
            return @{ Command = $resolved.Source; Prefix = @($candidate.Prefix) }
        }
    }
    throw "Python 3.11 or newer was not found. Install 64-bit Python from python.org and enable the Python launcher."
}

function Invoke-PythonLauncher($Launcher, [string[]]$Arguments) {
    $allArguments = @($Launcher.Prefix) + $Arguments
    & $Launcher.Command @allArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Python command failed with exit code $LASTEXITCODE"
    }
}

if (-not (Test-IsWindows)) {
    throw "This installer supports Windows only. Use install.sh on macOS."
}
if ($EnableAutoUpdate -and $NoAutoUpdate) {
    throw "EnableAutoUpdate and NoAutoUpdate cannot be used together."
}

$packageRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not (Test-Path -LiteralPath (Join-Path $packageRoot "app") -PathType Container)) {
    throw "Package is incomplete: app directory is missing."
}

if ([string]::IsNullOrWhiteSpace($InstallDir)) {
    if (-not [string]::IsNullOrWhiteSpace($env:LOCAL_KNOWLEDGE_HOME)) {
        $InstallDir = $env:LOCAL_KNOWLEDGE_HOME
    } else {
        $InstallDir = Join-Path $env:LOCALAPPDATA "LocalKnowledgeHub"
    }
}
$installRoot = Assert-SafeInstallRoot $InstallDir
$launcher = Get-PythonLauncher

New-Item -ItemType Directory -Force -Path $installRoot | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $installRoot "data") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $installRoot "bin") | Out-Null

$newApp = Join-Path $installRoot ".app.new.$PID"
$previousApp = Join-Path $installRoot ".app.previous"
$installedApp = Join-Path $installRoot "app"
if (Test-Path -LiteralPath $newApp) {
    Remove-Item -LiteralPath $newApp -Recurse -Force
}
Copy-Item -LiteralPath (Join-Path $packageRoot "app") -Destination $newApp -Recurse -Force
if (Test-Path -LiteralPath $previousApp) {
    Remove-Item -LiteralPath $previousApp -Recurse -Force
}
try {
    if (Test-Path -LiteralPath $installedApp) {
        Move-Item -LiteralPath $installedApp -Destination $previousApp
    }
    Move-Item -LiteralPath $newApp -Destination $installedApp
} catch {
    if ((-not (Test-Path -LiteralPath $installedApp)) -and (Test-Path -LiteralPath $previousApp)) {
        Move-Item -LiteralPath $previousApp -Destination $installedApp
    }
    throw
}

if (Test-Path -LiteralPath (Join-Path $packageRoot "docs\usage-quality.md") -PathType Leaf) {
    $docsRoot = Join-Path $installRoot "docs"
    New-Item -ItemType Directory -Force -Path $docsRoot | Out-Null
    Copy-Item -LiteralPath (Join-Path $packageRoot "docs\usage-quality.md") -Destination (Join-Path $docsRoot "usage-quality.md") -Force
}
foreach ($name in @("uninstall.ps1", "README.md", "LICENSE", "THIRD_PARTY_NOTICES.md")) {
    Copy-Item -LiteralPath (Join-Path $packageRoot $name) -Destination (Join-Path $installRoot $name) -Force
}

$venvPython = Join-Path $installRoot "venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    Invoke-PythonLauncher $launcher @("-m", "venv", (Join-Path $installRoot "venv"))
}
if (-not $SkipPythonDeps) {
    & $venvPython -m pip install --disable-pip-version-check -r (Join-Path $installedApp "requirements.txt")
    if ($LASTEXITCODE -ne 0) {
        throw "Python dependency installation failed."
    }
}

$manager = Join-Path $installedApp "src\install_manager.py"
$managerArgs = @(
    $manager,
    "initialize",
    "--install-root", $installRoot,
    "--source-app", $installedApp
)
if ($WithoutServices) {
    $managerArgs += "--without-services"
}
if ($EnableAutoUpdate) {
    $managerArgs += "--auto-update"
} elseif ($NoAutoUpdate) {
    $managerArgs += "--no-auto-update"
}
if ($WithAntigravityIDE) {
    $managerArgs += "--with-antigravity-ide"
}
& $venvPython @managerArgs
if ($LASTEXITCODE -ne 0) {
    throw "Local client configuration failed."
}

$dataRootMarker = Join-Path $installRoot ".knowledge-hub-data-root"
if (-not (Test-Path -LiteralPath $dataRootMarker -PathType Leaf)) {
    throw "Installer did not select a knowledge data root."
}
$selectedDataRoot = (Get-Content -LiteralPath $dataRootMarker -TotalCount 1).Trim()
if ([string]::IsNullOrWhiteSpace($selectedDataRoot)) {
    throw "Installer selected an empty knowledge data root."
}
$env:KHUB_DATA_DIR = $selectedDataRoot
$khub = Join-Path $installRoot "bin\khub.cmd"
& $khub init | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Knowledge database initialization failed."
}
& $venvPython (Join-Path $installedApp "src\discover_projects.py")
if ($LASTEXITCODE -ne 0) {
    throw "Project discovery failed."
}
$catalogArgs = @((Join-Path $installedApp "src\export_mcp_catalog.py"))
if ($WithAntigravityIDE) {
    $catalogArgs += @(
        (Join-Path $env:USERPROFILE ".gemini\antigravity\mcp\local-knowledge"),
        (Join-Path $env:USERPROFILE ".gemini\antigravity-ide\mcp\local-knowledge")
    )
}
& $venvPython @catalogArgs | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "MCP catalog export failed."
}

if ((-not $WithoutServices) -and (-not $NoStart)) {
    & $venvPython (Join-Path $installedApp "src\start_services.py") --once
    if ($LASTEXITCODE -ne 0) {
        throw "Docker services failed to start."
    }
}

& (Join-Path $installRoot "bin\knowledge-hub-doctor.cmd")
if ($LASTEXITCODE -ne 0) {
    throw "Installation health check failed."
}

Write-Host ""
Write-Host "Installed Local Knowledge Hub at:"
Write-Host "  $installRoot"
Write-Host "Restart Codex and Antigravity to load local-knowledge."
if (-not $WithoutServices) {
    Write-Host "Onyx: http://127.0.0.1:3000"
    Write-Host "First-account credentials: $(Join-Path $selectedDataRoot 'config\admin.env')"
}
