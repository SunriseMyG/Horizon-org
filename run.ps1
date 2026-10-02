# Install the dependencies and start the Horizon Discord/GitHub bot.
[CmdletBinding()]
param(
    # Skip the dependency step and start the bot right away.
    [switch] $NoInstall
)

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

$venv = '.venv'
$requirements = 'requirements.txt'
$lock = Join-Path $venv '.requirements.lock'
$requiredKeys = @('DISCORD_TOKEN', 'DISCORD_CHANNEL_ID', 'GITHUB_TOKEN', 'GITHUB_PROJECT_ID')

function Fail($message) {
    Write-Host "Error: $message" -ForegroundColor Red
    exit 1
}

function Find-Python {
    foreach ($candidate in @('python', 'py', 'python3')) {
        $command = Get-Command $candidate -ErrorAction SilentlyContinue
        if (-not $command) { continue }
        # The Microsoft Store stub resolves but exits without running anything.
        & $candidate -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>$null
        if ($LASTEXITCODE -eq 0) { return $candidate }
    }
    return $null
}

function Get-VenvPython {
    $path = Join-Path $venv 'Scripts\python.exe'
    if (Test-Path $path) { return $path }
    $path = Join-Path $venv 'bin/python'
    if (Test-Path $path) { return $path }
    return $null
}

function Get-EnvValue($file, $key) {
    foreach ($line in Get-Content $file) {
        if ($line -match "^\s*$key\s*=(.*)$") { return $Matches[1].Trim() }
    }
    return ''
}

# --- virtual environment -----------------------------------------------
if (-not (Get-VenvPython)) {
    $pythonBin = Find-Python
    if (-not $pythonBin) {
        Fail 'Python 3.10+ not found. Install it from https://www.python.org/downloads/'
    }
    Write-Host "==> Creating $venv with $pythonBin" -ForegroundColor Cyan
    & $pythonBin -m venv $venv
    if ($LASTEXITCODE -ne 0) { Fail "could not create $venv" }
}
$py = Get-VenvPython
if (-not $py) { Fail "$venv looks broken. Delete it and run this script again." }

# --- dependencies ------------------------------------------------------
if (-not $NoInstall) {
    $upToDate = (Test-Path $lock) -and
        ((Get-FileHash $requirements).Hash -eq (Get-FileHash $lock).Hash)
    if ($upToDate) {
        Write-Host '==> Dependencies already up to date' -ForegroundColor Cyan
    }
    else {
        Write-Host "==> Installing $requirements" -ForegroundColor Cyan
        & $py -m pip install --quiet --disable-pip-version-check -r $requirements
        if ($LASTEXITCODE -ne 0) { Fail 'dependency installation failed' }
        Copy-Item $requirements $lock -Force
    }
}

# --- configuration -----------------------------------------------------
if (-not (Test-Path '.env')) {
    Copy-Item '.env.example' '.env'
    Fail '.env was missing, it has been created from .env.example. Fill it in and run this script again.'
}

$missing = $requiredKeys | Where-Object {
    $value = Get-EnvValue '.env' $_
    $example = Get-EnvValue '.env.example' $_
    (-not $value) -or ($example -and $value -eq $example)
}
if ($missing) {
    Fail "these .env variables still hold their placeholder value: $($missing -join ', ')"
}

# --- run ---------------------------------------------------------------
Write-Host '==> Starting the bot (Ctrl+C to stop)' -ForegroundColor Cyan
& $py -m bot_horizon.main
exit $LASTEXITCODE
