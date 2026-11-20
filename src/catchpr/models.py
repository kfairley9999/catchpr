"""Shared data types for a catchpr run."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Symbol:
    qualname: str
    name: str
    kind: str
    start_line: int
    end_line: int
    source: str


@dataclass(frozen=True)
class FileChange:
    path: str
    status: str
    old_path: str | None
    base_source: str | None
    head_source: str | None
    base_symbols: tuple[Symbol, ...]
    head_symbols: tuple[Symbol, ...]
    changed_qualnames: tuple[str, ...]
    added_qualnames: tuple[str, ...]
    removed_qualnames: tuple[str, ...]
    renamed: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class DiffAnalysis:
    repo: str
    base_sha: str
    head_sha: str
    title: str
    body: str
    files: tuple[FileChange, ...]

    def changed_names(self) -> set[str]:
        names: set[str] = set()
        for item in self.files:
            for qual in (
                *item.changed_qualnames,
                *item.added_qualnames,
                *item.removed_qualnames,
            ):
                names.add(qual)
                names.add(qual.rsplit(".", 1)[-1])
            for old, new in item.renamed:
                names.add(old)
                names.add(old.rsplit(".", 1)[-1])
                names.add(new)
                names.add(new.rsplit(".", 1)[-1])
        return names

    def removed_names(self) -> set[str]:
        names: set[str] = set()
        for item in self.files:
            for qual in item.removed_qualnames:
                names.add(qual)
                names.add(qual.rsplit(".", 1)[-1])
            for old, _new in item.renamed:
                names.add(old)
                names.add(old.rsplit(".", 1)[-1])
        return names

    def added_names(self) -> set[str]:
        names: set[str] = set()
        for item in self.files:
            for qual in item.added_qualnames:
                names.add(qual)
                names.add(qual.rsplit(".", 1)[-1])
            for _old, new in item.renamed:
                names.add(new)
                names.add(new.rsplit(".", 1)[-1])
        return names

    def primary_location(self) -> tuple[str, int]:
        for item in self.files:
            if item.changed_qualnames and item.head_symbols:
                wanted = item.changed_qualnames[0]
                for sym in item.head_symbols:
                    if sym.qualname == wanted:
                        return item.path, sym.start_line
            if item.head_symbols:
                return item.path, item.head_symbols[0].start_line
            if item.path:
                return item.path, 1
        return "UNKNOWN", 1


@dataclass(frozen=True)
class Mutant:
    path: str
    symbol: str
    replacement: str


@dataclass(frozen=True)
class GeneratedTest:
    workflow: str
    test_id: str
    rationale: str
    source: str
    mutant: Mutant | None
    target_path: str | None


@dataclass(frozen=True)
class ProcResult:
    passed: bool
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_s: float

    def combined(self) -> str:
        parts = [self.stdout.strip(), self.stderr.strip()]
        return "\n".join(part for part in parts if part)


@dataclass
class Usage:
    logical_calls: int = 0
    remote_calls: int = 0
    cache_hits: int = 0
    prompt_chars: int = 0
    remote_prompt_chars: int = 0
    completion_chars: int = 0
    retries: int = 0


@dataclass(frozen=True)
class Rejection:
    filter_name: str
    reason: str
    test_id: str
    workflow: str


@dataclass
class Catch:
    test: GeneratedTest
    base_runs: tuple[ProcResult, ...]
    head_runs: tuple[ProcResult, ...]
    mutant_run: ProcResult | None
    location_path: str
    location_line: int
    reproduce: str


@dataclass
class Report:
    base_sha: str
    head_sha: str
    title: str
    body: str
    catches: list[Catch] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def silent(self) -> bool:
        return not self.catches
