[CmdletBinding()]
param(
    [switch]$SkipRust
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if ($env:PROCESSOR_ARCHITECTURE -ne "AMD64") {
    throw "Unsupported architecture: $env:PROCESSOR_ARCHITECTURE. The prebuilt Rust engine requires Windows x64."
}

if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3.11 -c "import sys; assert sys.version_info >= (3, 10), 'Python 3.10+ is required'"
    if ($LASTEXITCODE -ne 0) {
        throw "Python 3.11 was not found through the Windows Python launcher."
    }
    & py -3.11 -m venv .venv
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    & python -c "import sys; assert sys.version_info >= (3, 10), 'Python 3.10+ is required'"
    if ($LASTEXITCODE -ne 0) {
        throw "Python 3.10 or newer is required."
    }
    & python -m venv .venv
} else {
    throw "Python was not found. Install 64-bit Python 3.11 and run this script again."
}

$VenvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -r requirements.txt

if (-not $SkipRust) {
    Write-Host "Installing the precompiled Rust engine (private repository access may require GITHUB_TOKEN or GH_TOKEN)..."
    & $VenvPython install_rust_binary.py
}

& $VenvPython -c "import config; config.ensure_dirs(); print(f'Data root: {config.DATA_ROOT}'); print(f'OpenTrader root: {config.OPENTRADER_ROOT}')"

Write-Host "Setup complete."
Write-Host "Copy the production Database folder under: $HOME\Mark EMA and HTF\Database"
Write-Host "Store Screener.in credentials at: $HOME\Mark EMA and HTF\Screener login\screener.txt"
Write-Host "Then run: .\run_windows.cmd"
