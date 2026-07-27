# Precompiled Rust engine

Linux and Windows users do not need Rust, Cargo, CMake, or a C++ compiler.
The platform setup scripts install the Python runtime and then call the binary
installer:

```powershell
.\setup_windows.ps1
```

```bash
./setup_linux.sh
```

After setting up Python manually, `python install_rust_binary.py` remains
available.

The installer uses only the Python standard library. It downloads the newest
`rust-v*` GitHub Release for the current x86_64 platform, verifies the
published SHA-256 checksum, and installs the executable under:

```text
stock_analysis/bin/linux-x86_64/stock_analysis
stock_analysis/bin/windows-x86_64/stock_analysis.exe
```

`config.py` discovers that path automatically. `MARK_RUST_BINARY` can still be
used to select a different executable explicitly.

The chart fonts are embedded in the binary from the redistributable DejaVu
font files, so no operating-system font package is required.

Because this repository is private, the installer needs repository read
access. It automatically uses either `GITHUB_TOKEN`, `GH_TOKEN`, or an
existing authenticated GitHub CLI session. On a Python-only machine, set a
token with repository Contents read permission before running the installer:

```bash
export GITHUB_TOKEN="your-token"       # Linux
set GITHUB_TOKEN=your-token            # Windows Command Prompt
```

## GitHub Actions

`.github/workflows/build-rust-binaries.yml` compiles and smoke-tests both
native targets:

- Ubuntu 22.04: `x86_64-unknown-linux-gnu`
- Windows Server 2022: `x86_64-pc-windows-msvc`

The Windows build statically links its C runtime, avoiding a separate Visual
C++ Redistributable installation.

Pull requests and relevant pushes receive downloadable 30-day workflow
artifacts. Pushing a `rust-v*` tag additionally creates a durable GitHub
Release containing both archives and their checksum files.
