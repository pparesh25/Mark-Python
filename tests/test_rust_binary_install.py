from pathlib import Path

import config
import install_rust_binary


def test_installer_selects_supported_platform_keys():
    assert install_rust_binary.platform_key("Linux", "x86_64") == "linux-x86_64"
    assert install_rust_binary.platform_key("Windows", "AMD64") == "windows-x86_64"


def test_installer_rejects_unpublished_platform():
    try:
        install_rust_binary.platform_key("Darwin", "arm64")
    except RuntimeError as error:
        assert "Supported platforms" in str(error)
    else:
        raise AssertionError("unsupported platform should not select an asset")


def test_config_prefers_downloaded_prebuilt_binary(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BASE_DIR", tmp_path)
    monkeypatch.setattr(config.platform, "system", lambda: "Windows")
    monkeypatch.setattr(config.platform, "machine", lambda: "AMD64")
    binary = (
        tmp_path
        / "stock_analysis"
        / "bin"
        / "windows-x86_64"
        / "stock_analysis.exe"
    )
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"binary")

    assert config._resolve_rust_binary() == binary


def test_config_honors_explicit_binary_override(tmp_path, monkeypatch):
    override = tmp_path / "custom-rust-engine"
    monkeypatch.setenv("MARK_RUST_BINARY", str(override))

    assert config._resolve_rust_binary() == Path(override)


def test_installer_uses_environment_token_before_cli(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "environment-token")
    monkeypatch.setattr(
        install_rust_binary.shutil,
        "which",
        lambda executable: "/usr/bin/gh",
    )

    assert install_rust_binary._github_token() == "environment-token"


def test_installer_can_run_without_token_for_public_repository(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(install_rust_binary.shutil, "which", lambda executable: None)

    assert install_rust_binary._github_token() is None
