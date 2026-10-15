[CmdletBinding()]
param(
    [switch]$SkipRust
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if ($env:PROCESSOR_ARCHITECTURE -ne "AMD64") {
    throw "Unsupported architecture: $env:PROCESSOR_ARCHITECTURE. The prebuilt Rust engine requires Windows x64."
}

# numpy 2.2.6 (pinned in requirements.txt) has wheels for Python 3.10-3.13 only;
# 3.11 is preferred, then 3.12.
$VersionCheck = "import sys; sys.exit(0 if (3, 10) <= sys.version_info[:2] <= (3, 13) else 1)"
$Created = $false
if (Get-Command py -ErrorAction SilentlyContinue) {
    foreach ($Version in @("3.11", "3.12", "3.13", "3.10")) {
        # No stderr redirect: with ErrorActionPreference Stop it would turn "not installed" into an error.
        & py "-$Version" -c $VersionCheck
        if ($LASTEXITCODE -eq 0) {
            Write-Host "Using Python $Version through the Windows Python launcher."
            & py "-$Version" -m venv .venv
            if ($LASTEXITCODE -ne 0) { throw "Creating the virtual environment with Python $Version failed." }
            $Created = $true
            break
        }
    }
}
if (-not $Created -and (Get-Command python -ErrorAction SilentlyContinue)) {
    & python -c $VersionCheck
    if ($LASTEXITCODE -eq 0) {
        & python -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw "Creating the virtual environment failed." }
        $Created = $true
    } else {
        $Found = (& python --version)
        throw "Python 3.10-3.13 is required (found $Found); the pinned numpy 2.2.6 has no wheels for other versions. Install 64-bit Python 3.11 and run this script again."
    }
}
if (-not $Created) {
    throw "Python 3.10-3.13 was not found. Install 64-bit Python 3.11 and run this script again."
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
