#Requires -Version 5.1
<#
.SYNOPSIS
    Deploy the Hackathon 1 stack from PowerShell or cmd.

.DESCRIPTION
    Docker for this project runs inside WSL and is not on the Windows PATH, and
    the deploy logic is a bash script. Neither is usable directly from
    PowerShell, so this does not try: it translates this folder to its WSL path
    and runs scripts/deploy.sh in there, passing your terminal through so the
    one-time credential prompt still works.

    This is a thin wrapper on purpose. All the actual logic -- preflight,
    resource profiles, the health gate -- stays in scripts/deploy.sh, so there
    is one implementation and not two that drift apart.

.PARAMETER MemThresholdGb
    Override the lean/full cut-off (default 8 GB of total RAM). You almost
    never need this; the profile is chosen automatically.

.PARAMETER Full
    Force the full profile regardless of machine size. Shorthand for
    -MemThresholdGb 1. Useful for testing the full path on a small machine.

.PARAMETER DryRun
    Validate everything -- WSL present, path translation, Docker reachable --
    and print the command that would run, without starting any container.

.EXAMPLE
    .\scripts\deploy.ps1

.EXAMPLE
    .\scripts\deploy.ps1 -Full
#>
[CmdletBinding()]
param(
    [int]    $MemThresholdGb = 0,
    [switch] $Full,
    [switch] $DryRun
)

$ErrorActionPreference = 'Stop'

function Fail($message, $hint) {
    Write-Host "ERROR: $message" -ForegroundColor Red
    if ($hint) { Write-Host "       $hint" -ForegroundColor Yellow }
    exit 1
}

# --- 1. WSL must exist ------------------------------------------------------
$wsl = Get-Command wsl.exe -ErrorAction SilentlyContinue
if ($null -eq $wsl) {
    Fail "WSL was not found on this machine." `
         "This project's Docker runs inside WSL. Install it with:  wsl --install"
}

# --- 2. Translate this folder to its WSL path -------------------------------
# $PSScriptRoot is <repo>/HACKATHON_1/scripts; the compose files live one up.
$appDir = Split-Path -Parent $PSScriptRoot

# Hand wslpath FORWARD slashes. wsl.exe eats backslashes in its arguments as
# escapes, so passing the native path turns
#   C:\projects\ACCENTURE-HACKATHON-main\HACKATHON_1
# into
#   C:projectsACCENTURE-HACKATHON-mainHACKATHON_1
# and wslpath rejects it. wslpath accepts forward slashes on a Windows path.
$appDirFwd = $appDir.Replace('\', '/')
$wslDir = (& wsl.exe wslpath -a "$appDirFwd") 2>$null
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($wslDir)) {
    Fail "Could not translate '$appDir' to a WSL path." `
         "Is the WSL default distribution running? Try:  wsl -l -v"
}
$wslDir = $wslDir.Trim()

# --- 3. Docker must be usable inside WSL ------------------------------------
# Checked here rather than letting deploy.sh fail midway, so the message names
# the real problem instead of a bare 'docker: command not found'.
& wsl.exe -e bash -lc "command -v docker >/dev/null 2>&1" | Out-Null
if ($LASTEXITCODE -ne 0) {
    Fail "Docker is not available inside WSL." `
         "Start Docker Desktop and make sure WSL integration is enabled for your distro."
}

# --- 4. Build the command ---------------------------------------------------
if ($Full -and $MemThresholdGb -eq 0) { $MemThresholdGb = 1 }

$envPrefix = ''
if ($MemThresholdGb -gt 0) { $envPrefix = "MEM_THRESHOLD_GB=$MemThresholdGb " }

# Single-quote the directory for bash: Windows paths under /mnt/c routinely
# contain spaces, and an unquoted cd would split on them.
$command = "cd '$wslDir' && ${envPrefix}bash scripts/deploy.sh"

Write-Host "Running in WSL: $wslDir" -ForegroundColor Cyan
if ($MemThresholdGb -gt 0) {
    Write-Host "Profile threshold overridden: ${MemThresholdGb}GB" -ForegroundColor Cyan
}
if ($DryRun) {
    Write-Host "DRY RUN - nothing started. Would execute:" -ForegroundColor Yellow
    Write-Host "  wsl.exe -e bash -lc `"$command`""
    exit 0
}

Write-Host ""

# Call wsl.exe directly (not through bash -c "...") so stdin stays attached to
# this console. preflight.sh tests `[ -t 0 ]` to decide whether it may prompt
# for credentials; break that and a fresh checkout fails with "missing values"
# instead of asking you for them.
& wsl.exe -e bash -lc $command
$code = $LASTEXITCODE

if ($code -ne 0) {
    Write-Host ""
    Write-Host "Deploy failed (exit $code). The log for this run is under HACKATHON_1\logs\." -ForegroundColor Red
}
exit $code
