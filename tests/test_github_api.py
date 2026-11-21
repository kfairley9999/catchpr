"""Failure paths for the trusted GitHub comment job helpers."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
import zipfile
from email.message import EmailMessage
from pathlib import Path

import pytest

from catchpr.errors import GitHubError
from catchpr.github_api import (
    _safe_extract,
    fetch_artifact,
    find_sticky,
    post_sticky,
)


class _Response:
    def __init__(self, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self._body = body
        self.headers = EmailMessage()
        for key, value in (headers or {}).items():
            self.headers[key] = value

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def _install(monkeypatch: pytest.MonkeyPatch, handler):  # type: ignore[no-untyped-def]
    monkeypatch.setattr("catchpr.github_api.urllib.request.urlopen", handler)


def test_post_sticky_rejects_bad_inputs() -> None:
    with pytest.raises(GitHubError, match="positive"):
        post_sticky(repository="acme/widgets", pr=0, body="hi", token="token")
    with pytest.raises(GitHubError, match="empty"):
        post_sticky(repository="acme/widgets", pr=1, body="hi", token="")
    with pytest.raises(GitHubError, match="owner/name"):
        post_sticky(repository="widgets", pr=1, body="hi", token="token")


def test_find_sticky_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def urlopen(request: object, timeout: int = 60) -> _Response:
        raise urllib.error.HTTPError(
            "https://api.github.com/comments",
            500,
            "boom",
            EmailMessage(),
            io.BytesIO(b"nope"),
        )

    _install(monkeypatch, urlopen)
    with pytest.raises(GitHubError, match="listing comments failed: HTTP 500"):
        find_sticky("acme/widgets", 4, "token", "https://example.test")


def test_find_sticky_not_json(monkeypatch: pytest.MonkeyPatch) -> None:
    def urlopen(request: object, timeout: int = 60) -> _Response:
        return _Response(200, b"<html>nope")

    _install(monkeypatch, urlopen)
    with pytest.raises(GitHubError, match="not JSON"):
        find_sticky("acme/widgets", 4, "token", "https://example.test")


def test_find_sticky_unexpected_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    def urlopen(request: object, timeout: int = 60) -> _Response:
        return _Response(200, b'{"comments": []}')

    _install(monkeypatch, urlopen)
    with pytest.raises(GitHubError, match="unexpected shape"):
        find_sticky("acme/widgets", 4, "token", "https://example.test")


def test_find_sticky_paginates_and_skips_malformed_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def urlopen(request: urllib.request.Request, timeout: int = 60) -> _Response:
        url = request.full_url
        calls.append(url)
        if "page=2" not in url:
            headers = {"Link": '<https://example.test/comments?page=2>; rel="next"'}
            body = json.dumps([{"id": 1, "body": "no marker"}, "skip-me", {"id": "x"}]).encode()
            return _Response(200, body, headers)
        body = json.dumps(
            [
                {"id": 9, "body": "intro <!-- catchpr:sticky --> rest"},
            ]
        ).encode()
        return _Response(200, body)

    monkeypatch.setattr("catchpr.github_api.urllib.request.urlopen", urlopen)
    assert find_sticky("acme/widgets", 4, "token", "https://example.test") == 9
    assert any("page=2" in url for url in calls)


def test_fetch_artifact_rejects_bad_run_id() -> None:
    with pytest.raises(GitHubError, match="run id"):
        fetch_artifact(
            repository="acme/widgets",
            run_id=0,
            name="catchpr-results",
            dest=Path("/tmp"),
            token="token",
        )


def test_fetch_artifact_list_http_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def urlopen(request: object, timeout: int = 60) -> _Response:
        raise urllib.error.HTTPError(
            "https://example.test/artifacts",
            403,
            "denied",
            EmailMessage(),
            io.BytesIO(b"no"),
        )

    _install(monkeypatch, urlopen)
    with pytest.raises(GitHubError, match="listing artifacts failed: HTTP 403"):
        fetch_artifact(
            repository="acme/widgets",
            run_id=9,
            name="catchpr-results",
            dest=tmp_path,
            token="token",
            api="https://example.test",
        )


def test_fetch_artifact_list_not_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def urlopen(request: object, timeout: int = 60) -> _Response:
        return _Response(200, b"not-json")

    _install(monkeypatch, urlopen)
    with pytest.raises(GitHubError, match="not JSON"):
        fetch_artifact(
            repository="acme/widgets",
            run_id=9,
            name="catchpr-results",
            dest=tmp_path,
            token="token",
            api="https://example.test",
        )


def test_fetch_artifact_list_bad_shape(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def urlopen(request: object, timeout: int = 60) -> _Response:
        return _Response(200, b"[1, 2, 3]")

    _install(monkeypatch, urlopen)
    with pytest.raises(GitHubError, match="unexpected shape"):
        fetch_artifact(
            repository="acme/widgets",
            run_id=9,
            name="catchpr-results",
            dest=tmp_path,
            token="token",
            api="https://example.test",
        )


def test_fetch_artifact_missing_name(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def urlopen(request: object, timeout: int = 60) -> _Response:
        return _Response(200, json.dumps({"artifacts": [{"name": "other"}]}).encode())

    _install(monkeypatch, urlopen)
    with pytest.raises(GitHubError, match="was not found"):
        fetch_artifact(
            repository="acme/widgets",
            run_id=9,
            name="catchpr-results",
            dest=tmp_path,
            token="token",
            api="https://example.test",
        )


def test_fetch_artifact_download_http_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = {"n": 0}

    def urlopen(request: urllib.request.Request, timeout: int = 60) -> _Response:
        calls["n"] += 1
        if "artifacts" in request.full_url:
            body = json.dumps(
                {
                    "artifacts": [
                        {
                            "name": "catchpr-results",
                            "archive_download_url": "https://example.test/zip",
                        }
                    ]
                }
            ).encode()
            return _Response(200, body)
        raise urllib.error.HTTPError(
            request.full_url,
            410,
            "gone",
            EmailMessage(),
            io.BytesIO(b"gone"),
        )

    monkeypatch.setattr("catchpr.github_api.urllib.request.urlopen", urlopen)
    with pytest.raises(GitHubError, match="downloading artifact failed: HTTP 410"):
        fetch_artifact(
            repository="acme/widgets",
            run_id=9,
            name="catchpr-results",
            dest=tmp_path,
            token="token",
            api="https://example.test",
        )
    assert calls["n"] == 2


def test_fetch_artifact_network_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def urlopen(request: object, timeout: int = 60) -> _Response:
        raise urllib.error.URLError("timed out")

    _install(monkeypatch, urlopen)
    with pytest.raises(GitHubError, match="GitHub request failed"):
        fetch_artifact(
            repository="acme/widgets",
            run_id=9,
            name="catchpr-results",
            dest=tmp_path,
            token="token",
            api="https://example.test",
        )


def test_post_sticky_write_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def urlopen(request: urllib.request.Request, timeout: int = 60) -> _Response:
        if request.method == "GET":
            return _Response(200, b"[]")
        raise urllib.error.HTTPError(
            request.full_url,
            401,
            "denied",
            EmailMessage(),
            io.BytesIO(b'{"message":"bad token"}'),
        )

    monkeypatch.setattr("catchpr.github_api.urllib.request.urlopen", urlopen)
    with pytest.raises(GitHubError, match="GitHub HTTP 401"):
        post_sticky(
            repository="acme/widgets",
            pr=4,
            body="<!-- catchpr:sticky --> hello",
            token="token",
            api="https://example.test",
        )


def test_safe_extract_rejects_zip_slip(tmp_path: Path) -> None:
    blob = io.BytesIO()
    with zipfile.ZipFile(blob, "w") as archive:
        archive.writestr("../evil.txt", "nope")
    with pytest.raises(GitHubError, match="escapes destination"):
        _safe_extract(blob.getvalue(), tmp_path)
    assert not (tmp_path.parent / "evil.txt").exists()


def test_safe_extract_rejects_bad_zip(tmp_path: Path) -> None:
    with pytest.raises(GitHubError, match="not a zip"):
        _safe_extract(b"this is not a zip", tmp_path)


def test_safe_extract_accepts_directory_entries(tmp_path: Path) -> None:
    blob = io.BytesIO()
    with zipfile.ZipFile(blob, "w") as archive:
        archive.writestr("nested/", "")
        archive.writestr("nested/result.json", '{"ok": true}')
    root = _safe_extract(blob.getvalue(), tmp_path)
    assert (root / "nested" / "result.json").read_text(encoding="utf-8") == '{"ok": true}'
