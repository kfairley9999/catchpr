"""GitHub REST helpers used by the trusted comment job.

The comment job rebuilds markdown from the JSON artifact and posts it.
It never checks out pull-request code and never executes a generated test.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from catchpr.errors import GitHubError
from catchpr.jsonutil import as_dict, as_list, as_str, loads

MARKER = "<!-- catchpr:sticky -->"


def post_sticky(
    *,
    repository: str,
    pr: int,
    body: str,
    token: str,
    api: str = "https://api.github.com",
) -> str:
    """Create, update, or delete the sticky comment. Returns the action taken."""
    if pr < 1:
        raise GitHubError("pull request number must be positive")
    if not token:
        raise GitHubError("GITHUB_TOKEN is empty")
    owner_repo = _repository(repository)
    existing = find_sticky(owner_repo, pr, token, api)
    if not body.strip():
        if existing is None:
            return "silent"
        _request(
            "DELETE",
            f"{api}/repos/{owner_repo}/issues/comments/{existing}",
            token,
        )
        return "deleted"
    if existing is None:
        _request(
            "POST",
            f"{api}/repos/{owner_repo}/issues/{pr}/comments",
            token,
            {"body": body},
        )
        return "created"
    _request(
        "PATCH",
        f"{api}/repos/{owner_repo}/issues/comments/{existing}",
        token,
        {"body": body},
    )
    return "updated"


def find_sticky(repository: str, pr: int, token: str, api: str) -> int | None:
    url: str | None = f"{api}/repos/{repository}/issues/{pr}/comments?per_page=100"
    while url:
        status, headers, raw = _request("GET", url, token)
        if status >= 400:
            raise GitHubError(f"listing comments failed: HTTP {status}")
        try:
            payload = loads(raw)
        except json.JSONDecodeError as exc:
            raise GitHubError("comment list was not JSON") from exc
        rows = as_list(payload)
        if rows is None:
            raise GitHubError("comment list had an unexpected shape")
        for item in rows:
            data = as_dict(item)
            if data is None:
                continue
            text = as_str(data.get("body")) or ""
            if MARKER in text:
                number = data.get("id")
                if isinstance(number, int):
                    return number
        url = _next_link(headers.get("Link", ""))
    return None


def fetch_artifact(
    *,
    repository: str,
    run_id: int,
    name: str,
    dest: Path,
    token: str,
    api: str = "https://api.github.com",
) -> Path:
    """Download one workflow artifact zip and extract it. Does not execute files."""
    if run_id < 1:
        raise GitHubError("run id must be positive")
    owner_repo = _repository(repository)
    status, _headers, raw = _request(
        "GET",
        f"{api}/repos/{owner_repo}/actions/runs/{run_id}/artifacts?per_page=100",
        token,
    )
    if status >= 400:
        raise GitHubError(f"listing artifacts failed: HTTP {status}")
    try:
        payload = as_dict(loads(raw))
    except json.JSONDecodeError as exc:
        raise GitHubError("artifact list was not JSON") from exc
    if payload is None:
        raise GitHubError("artifact list had an unexpected shape")
    artifacts = as_list(payload.get("artifacts")) or []
    download: str | None = None
    for item in artifacts:
        data = as_dict(item)
        if data is None:
            continue
        if as_str(data.get("name")) == name:
            download = as_str(data.get("archive_download_url"))
            break
    if not download:
        raise GitHubError(f"artifact {name} was not found on run {run_id}")
    status, _headers, zip_bytes = _request("GET", download, token, binary=True)
    if status >= 400:
        raise GitHubError(f"downloading artifact failed: HTTP {status}")
    dest.mkdir(parents=True, exist_ok=True)
    blob = zip_bytes.encode("latin-1")
    return _safe_extract(blob, dest)


def _safe_extract(blob: bytes, dest: Path) -> Path:
    root = dest.resolve()
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            for info in archive.infolist():
                target = (root / info.filename).resolve()
                if target != root and root not in target.parents:
                    raise GitHubError(f"artifact path escapes destination: {info.filename}")
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(info))
    except zipfile.BadZipFile as exc:
        raise GitHubError("artifact was not a zip file") from exc
    return root


def _repository(value: str) -> str:
    cleaned = value.strip().strip("/")
    parts = cleaned.split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise GitHubError("repository must be owner/name")
    return cleaned


def _next_link(header: str) -> str | None:
    for piece in header.split(","):
        if 'rel="next"' not in piece:
            continue
        start = piece.find("<")
        end = piece.find(">")
        if start >= 0 and end > start:
            return piece[start + 1 : end]
    return None


def _request(
    method: str,
    url: str,
    token: str,
    payload: dict[str, object] | None = None,
    *,
    binary: bool = False,
) -> tuple[int, dict[str, str], str]:
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "catchpr",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
            headers = {key: value for key, value in response.headers.items()}
            if binary:
                return int(response.status), headers, raw.decode("latin-1")
            return int(response.status), headers, raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        headers = {key: value for key, value in exc.headers.items()}
        text = raw.decode("latin-1") if binary else raw.decode("utf-8", errors="replace")
        if exc.code >= 400 and method != "GET":
            raise GitHubError(f"GitHub HTTP {exc.code}: {text[:300]}") from exc
        return int(exc.code), headers, text
    except urllib.error.URLError as exc:
        raise GitHubError(f"GitHub request failed: {exc.reason}") from exc
