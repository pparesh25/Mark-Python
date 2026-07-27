# Precompiled Rust engine

Linux and Windows users do not need Rust, Cargo, CMake, or a C++ compiler.
After setting up Python, run:

```bash
python install_rust_binary.py
```

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
