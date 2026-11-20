"""Run a generated test in base and head worktrees.

A result counts only when the caller asks for it. This module runs one
process per attempt, with a timeout, resource limits, and network blocked
on Linux via unshare when the kernel allows it. A sitecustomize guard blocks
socket connects even when unshare is unavailable (including macOS).
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from catchpr.errors import GitError
from catchpr.gitutil import git
from catchpr.models import Mutant, ProcResult
from catchpr.mutate import MutantError, replace_symbol

_OUTPUT_LIMIT = 20_000

GUARD_SOURCE = '''"""Loaded automatically. Blocks outbound network from generated tests."""
import socket


def _deny(*_args: object, **_kwargs: object) -> object:
    raise OSError("catchpr sandbox: network is disabled")


socket.socket.connect = _deny  # type: ignore[method-assign]
socket.socket.connect_ex = lambda *_a, **_k: 1  # type: ignore[method-assign]
socket.create_connection = _deny  # type: ignore[assignment]
'''


def limit_specs(cpu_seconds: int, mem_bytes: int, file_bytes: int) -> list[tuple[int, int, int]]:
    import resource

    return [
        (resource.RLIMIT_CPU, cpu_seconds, cpu_seconds),
        (resource.RLIMIT_AS, mem_bytes, mem_bytes),
        (resource.RLIMIT_FSIZE, file_bytes, file_bytes),
        (resource.RLIMIT_CORE, 0, 0),
        (resource.RLIMIT_NOFILE, 256, 256),
    ]


def apply_limits(specs: list[tuple[int, int, int]]) -> None:  # pragma: no cover
    import resource

    for which, soft, hard in specs:
        try:
            resource.setrlimit(which, (soft, hard))
        except (ValueError, OSError):
            continue


def unshare_available() -> bool:
    if shutil.which("unshare") is None:
        return False
    try:
        proc = subprocess.run(
            ["unshare", "--user", "--map-root-user", "--net", "--", "true"],
            check=False,
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _scrub_env(base: dict[str, str], guard_dir: Path, worktree: Path) -> dict[str, str]:
    env = dict(base)
    for key in list(env):
        upper = key.upper()
        if (
            upper.endswith("_API_KEY")
            or upper.endswith("_TOKEN")
            or upper in {"GITHUB_TOKEN", "GH_TOKEN", "AWS_SECRET_ACCESS_KEY"}
        ):
            env.pop(key, None)
    prior = env.get("PYTHONPATH", "")
    parts = [str(guard_dir), str(worktree)]
    if prior:
        parts.append(prior)
    env["PYTHONPATH"] = os.pathsep.join(parts)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["CATCHPR_SANDBOX"] = "1"
    return env


class Sandbox:
    def __init__(
        self,
        repo: Path,
        base_sha: str,
        head_sha: str,
        *,
        timeout: float = 20.0,
        mem_bytes: int = 2 * 1024 * 1024 * 1024,
        file_bytes: int = 8 * 1024 * 1024,
        block_network: bool = True,
    ) -> None:
        self.repo = repo
        self.base_sha = base_sha
        self.head_sha = head_sha
        self.timeout = timeout
        self.mem_bytes = mem_bytes
        self.file_bytes = file_bytes
        self.block_network = block_network
        self._root: Path | None = None
        self._base: Path | None = None
        self._head: Path | None = None
        self._guard: Path | None = None
        self.network_unshare = False

    def __enter__(self) -> Sandbox:
        root = Path(tempfile.mkdtemp(prefix="catchpr-"))
        self._root = root
        guard = root / "guard"
        guard.mkdir()
        (guard / "sitecustomize.py").write_text(GUARD_SOURCE, encoding="utf-8")
        self._guard = guard
        self._base = root / "base"
        self._head = root / "head"
        git(self.repo, "worktree", "add", "--detach", str(self._base), self.base_sha)
        git(self.repo, "worktree", "add", "--detach", str(self._head), self.head_sha)
        self.network_unshare = self.block_network and unshare_available()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        for path in (self._base, self._head):
            if path is not None:
                try:
                    git(self.repo, "worktree", "remove", "--force", str(path))
                except GitError:
                    shutil.rmtree(path, ignore_errors=True)
        if self._root is not None:
            shutil.rmtree(self._root, ignore_errors=True)
        self._root = None

    def run_base(self, source: str) -> ProcResult:
        if self._base is None:
            raise RuntimeError("sandbox is not open")
        return self._run(self._base, source)

    def run_head(self, source: str) -> ProcResult:
        if self._head is None:
            raise RuntimeError("sandbox is not open")
        return self._run(self._head, source)

    def run_mutant(self, mutant: Mutant, source: str) -> ProcResult:
        if self._base is None or self._root is None:
            raise RuntimeError("sandbox is not open")
        scratch = self._root / "mutant"
        if scratch.exists():
            shutil.rmtree(scratch)
        shutil.copytree(self._base, scratch, symlinks=True)
        target = scratch / mutant.path
        if not target.is_file():
            return ProcResult(False, 1, "", f"mutant path missing: {mutant.path}", False, 0.0)
        try:
            original = target.read_text(encoding="utf-8")
            updated = replace_symbol(original, mutant.symbol, mutant.replacement)
        except (OSError, MutantError) as exc:
            return ProcResult(False, 1, "", f"mutant not applied: {exc}", False, 0.0)
        target.write_text(updated, encoding="utf-8")
        return self._run(scratch, source)

    def _run(self, worktree: Path, source: str) -> ProcResult:
        if self._guard is None:
            raise RuntimeError("sandbox is not open")
        probe = worktree / "_catchpr_probe"
        probe.mkdir(exist_ok=True)
        test_path = probe / "test_catchpr_probe.py"
        test_path.write_text(source, encoding="utf-8")
        pytest_cmd = [
            sys.executable,
            "-m",
            "pytest",
            str(test_path),
            "-p",
            "no:cacheprovider",
            "--noconftest",
            "-o",
            "addopts=",
            "--tb=short",
            "-q",
            "--disable-warnings",
        ]
        command = pytest_cmd
        if self.network_unshare:
            command = [
                "unshare",
                "--user",
                "--map-root-user",
                "--net",
                "--",
                *pytest_cmd,
            ]
        cpu = max(1, int(self.timeout))
        specs = limit_specs(cpu, self.mem_bytes, self.file_bytes)
        env = _scrub_env(dict(os.environ), self._guard, worktree)
        started = time.monotonic()
        timed_out = False
        try:
            proc = subprocess.Popen(
                command,
                cwd=worktree,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
                preexec_fn=lambda: apply_limits(specs),
            )
        except OSError as exc:
            return ProcResult(False, 1, "", f"failed to start pytest: {exc}", False, 0.0)
        try:
            stdout, stderr = proc.communicate(timeout=self.timeout + 2)
            code = proc.returncode if proc.returncode is not None else 1
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(proc.pid)
            stdout, stderr = proc.communicate()
            code = proc.returncode if proc.returncode is not None else -9
        duration = time.monotonic() - started
        return ProcResult(
            passed=code == 0 and not timed_out,
            returncode=code,
            stdout=_clip(stdout),
            stderr=_clip(stderr),
            timed_out=timed_out,
            duration_s=duration,
        )


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            return


def _clip(text: str) -> str:
    if len(text) <= _OUTPUT_LIMIT:
        return text
    return text[:_OUTPUT_LIMIT] + "\n... truncated ..."
