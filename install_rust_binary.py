"""Install the latest prebuilt stock_analysis binary from GitHub Releases.

This script uses only the Python standard library. It selects the correct
Windows/Linux x86_64 asset, verifies its SHA-256 checksum, and extracts it into
the location automatically discovered by ``config.RUST_BINARY``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import stat
import tarfile
import tempfile
from urllib.request import Request, urlopen
import zipfile

DEFAULT_REPOSITORY = "pparesh25/Mark-Python"
RELEASE_PREFIX = "rust-v"


def platform_key(system_name: str | None = None, machine_name: str | None = None) -> str:
    system = (system_name or platform.system()).strip().lower()
    machine = (machine_name or platform.machine()).strip().lower()
    architecture = {
        "amd64": "x86_64",
        "x64": "x86_64",
        "x86_64": "x86_64",
    }.get(machine)
    if system not in {"linux", "windows"} or architecture is None:
        raise RuntimeError(
            f"No prebuilt Rust binary is published for {system}/{machine}. "
            "Supported platforms: Linux x86_64 and Windows x86_64."
        )
    return f"{system}-{architecture}"


def _get_json(url: str):
    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "Mark-Python-Rust-Installer",
        },
    )
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def _download(url: str, destination: Path) -> None:
    request = Request(url, headers={"User-Agent": "Mark-Python-Rust-Installer"})
    with urlopen(request, timeout=120) as response:
        destination.write_bytes(response.read())


def _select_release(repository: str, requested_tag: str | None):
    releases = _get_json(
        f"https://api.github.com/repos/{repository}/releases?per_page=30"
    )
    for release in releases:
        tag = str(release.get("tag_name", ""))
        if release.get("draft"):
            continue
        if requested_tag and tag == requested_tag:
            return release
        if not requested_tag and tag.startswith(RELEASE_PREFIX):
            return release
    wanted = requested_tag or f"latest {RELEASE_PREFIX}* release"
    raise RuntimeError(f"Could not find {wanted} in {repository}")


def _asset_urls(release, key: str) -> tuple[str, str, str]:
    extension = "zip" if key.startswith("windows-") else "tar.gz"
    archive_name = f"stock-analysis-{key}.{extension}"
    checksum_name = f"{archive_name}.sha256"
    urls = {
        asset["name"]: asset["browser_download_url"]
        for asset in release.get("assets", [])
    }
    if archive_name not in urls or checksum_name not in urls:
        raise RuntimeError(
            f"Release {release.get('tag_name')} does not contain "
            f"{archive_name} and its checksum"
        )
    return archive_name, urls[archive_name], urls[checksum_name]


def _safe_extract_tar(archive: Path, destination: Path) -> None:
    with tarfile.open(archive, "r:gz") as bundle:
        for member in bundle.getmembers():
            target = (destination / member.name).resolve()
            if destination.resolve() not in target.parents and target != destination.resolve():
                raise RuntimeError(f"Unsafe archive member: {member.name}")
        bundle.extractall(destination)


def _safe_extract_zip(archive: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            target = (destination / member.filename).resolve()
            if destination.resolve() not in target.parents and target != destination.resolve():
                raise RuntimeError(f"Unsafe archive member: {member.filename}")
        bundle.extractall(destination)


def install(
    repository: str = DEFAULT_REPOSITORY,
    requested_tag: str | None = None,
    install_root: Path | None = None,
) -> Path:
    key = platform_key()
    root = (install_root or Path(__file__).resolve().parent).resolve()
    release = _select_release(repository, requested_tag)
    archive_name, archive_url, checksum_url = _asset_urls(release, key)

    with tempfile.TemporaryDirectory(prefix="mark-rust-install-") as temp_name:
        temp = Path(temp_name)
        archive = temp / archive_name
        checksum = temp / f"{archive_name}.sha256"
        _download(archive_url, archive)
        _download(checksum_url, checksum)

        expected = checksum.read_text(encoding="utf-8").split()[0].lower()
        actual = hashlib.sha256(archive.read_bytes()).hexdigest()
        if actual != expected:
            raise RuntimeError(
                f"Checksum mismatch for {archive_name}: expected {expected}, got {actual}"
            )

        if archive_name.endswith(".zip"):
            _safe_extract_zip(archive, root)
        else:
            _safe_extract_tar(archive, root)

    executable_name = "stock_analysis.exe" if key.startswith("windows-") else "stock_analysis"
    binary = root / "stock_analysis" / "bin" / key / executable_name
    if not binary.is_file():
        raise RuntimeError(f"Release extracted without expected binary: {binary}")
    if os.name != "nt":
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print(f"Installed {release['tag_name']}: {binary}")
    return binary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Install the prebuilt Mark screener Rust binary."
    )
    parser.add_argument("--repository", default=DEFAULT_REPOSITORY)
    parser.add_argument("--release", help="Specific rust-v* release tag")
    parser.add_argument(
        "--install-root",
        type=Path,
        help="Repository root; defaults to the directory containing this script",
    )
    args = parser.parse_args()
    install(args.repository, args.release, args.install_root)


if __name__ == "__main__":
    main()
