"""Tests for the HTTP-only Screener.in fundamental downloader."""

from pathlib import Path

import fundamental_downloader


class FakeResponse:
    def __init__(self, *, text="", url="", content=b"", content_type="text/html"):
        self.text = text
        self.url = url
        self.content = content
        self.headers = {"Content-Type": content_type}

    def raise_for_status(self):
        return None


class FakeCookies(dict):
    def set(self, name, value, **_kwargs):
        self[name] = value


class FakeSession:
    def __init__(self):
        self.headers = {}
        self.cookies = FakeCookies()
        self.timeouts = []

    def post(self, url, *, data, headers, timeout):
        self.timeouts.append(timeout)
        if "/login/" in url:
            return FakeResponse(url="https://www.screener.in/")
        return FakeResponse(
            content=b"Name,ISIN Code\nExample,INE000000001\n",
            content_type="text/csv",
        )


def test_http_download_uses_optimized_two_request_flow(tmp_path, monkeypatch):
    credentials = tmp_path / "screener.txt"
    credentials.write_text("person@example.com\nsecret\n", encoding="utf-8")
    monkeypatch.setattr(fundamental_downloader.config, "SCREENER_CREDS", credentials)
    session = FakeSession()

    output = tmp_path / "all-company.csv"
    assert fundamental_downloader._download_via_http(
        output,
        session=session,
    )

    assert output.read_bytes().startswith(b"Name,ISIN Code")
    assert len(session.timeouts) == 2
    assert session.timeouts == [
        fundamental_downloader.LOGIN_TIMEOUT_SECONDS,
        fundamental_downloader.EXPORT_TIMEOUT_SECONDS,
    ]


def test_invalid_export_does_not_replace_existing_file(tmp_path, monkeypatch):
    credentials = tmp_path / "screener.txt"
    credentials.write_text("person@example.com\nsecret\n", encoding="utf-8")
    monkeypatch.setattr(fundamental_downloader.config, "SCREENER_CREDS", credentials)
    output = tmp_path / "all-company.csv"
    output.write_bytes(b"existing-good-file")
    session = FakeSession()

    original_post = session.post

    def invalid_export(url, *, data, headers, timeout):
        if "/login/" in url:
            return original_post(
                url,
                data=data,
                headers=headers,
                timeout=timeout,
            )
        return FakeResponse(
            content=b"<html>server error</html>",
            content_type="text/html",
        )

    session.post = invalid_export
    try:
        fundamental_downloader._download_via_http(output, session=session)
    except RuntimeError:
        pass
    else:
        raise AssertionError("Expected invalid export content to be rejected")

    assert output.read_bytes() == b"existing-good-file"
    assert not Path(f"{output}.part").exists()
