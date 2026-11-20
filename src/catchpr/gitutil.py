"""Small git wrappers. Every call is a subprocess; nothing shells out via a string."""

from __future__ import annotations

import subprocess
from pathlib import Path

from catchpr.errors import GitError


def git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    if check and proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
        raise GitError(f"git {' '.join(args)} failed: {detail}")
    return proc.stdout


def rev_parse(repo: Path, rev: str) -> str:
    return git(repo, "rev-parse", "--verify", rev).strip()


def show_file(repo: Path, sha: str, path: str) -> str | None:
    proc = subprocess.run(
        ["git", "show", f"{sha}:{path}"],
        cwd=repo,
        check=False,
        capture_output=True,
    )
    if proc.returncode != 0:
        return None
    if b"\x00" in proc.stdout:
        return None
    return proc.stdout.decode("utf-8", errors="replace")


def ensure_repo(repo: Path) -> None:
    if not (repo / ".git").exists():
        inside = git(repo, "rev-parse", "--is-inside-work-tree").strip()
        if inside != "true":
            raise GitError(f"{repo} is not a git repository")
