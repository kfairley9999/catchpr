"""Map a git diff onto Python functions and classes with the ast module."""

from __future__ import annotations

import ast
import difflib
import re
from pathlib import Path

from catchpr.gitutil import git, rev_parse, show_file
from catchpr.models import DiffAnalysis, FileChange, Symbol

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_RENAME_SCORE = 0.72


def import_name(path: str) -> str:
    cleaned = path[:-3] if path.endswith(".py") else path
    parts = [part for part in cleaned.split("/") if part and part != "__init__"]
    return ".".join(parts) if parts else path


def parse_name_status(text: str) -> list[tuple[str, str, str | None]]:
    """Return (status, path, old_path) rows from ``git diff --name-status -M``."""
    rows: list[tuple[str, str, str | None]] = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        parts = raw.split("\t")
        code = parts[0]
        status = code[:1]
        if status in {"R", "C"} and len(parts) >= 3:
            rows.append(("renamed" if status == "R" else "copied", parts[2], parts[1]))
        elif status == "A" and len(parts) >= 2:
            rows.append(("added", parts[1], None))
        elif status == "D" and len(parts) >= 2:
            rows.append(("deleted", parts[1], None))
        elif status == "M" and len(parts) >= 2:
            rows.append(("modified", parts[1], None))
    return rows


def parse_changed_lines(diff_text: str) -> set[int]:
    """Line numbers in the new file that a unified diff adds or replaces."""
    changed: set[int] = set()
    new_line = 0
    active = False
    for line in diff_text.splitlines():
        match = _HUNK.match(line)
        if match:
            new_line = int(match.group(1))
            active = True
            continue
        if not active:
            continue
        if line.startswith("+++") or line.startswith("---") or line.startswith("\\"):
            continue
        if line.startswith("+"):
            changed.add(new_line)
            new_line += 1
        elif line.startswith("-"):
            continue
        else:
            new_line += 1
    return changed


def extract_symbols(source: str) -> tuple[Symbol, ...]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ()
    lines = source.splitlines()
    found: list[Symbol] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self._add(node, "class")
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._add(node, "function")
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._add(node, "async_function")
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def _add(self, node: ast.AST, kind: str) -> None:
            name = getattr(node, "name", "")
            if not isinstance(name, str) or not name:
                return
            start = int(getattr(node, "lineno", 1))
            end_raw = getattr(node, "end_lineno", None)
            end = int(end_raw) if isinstance(end_raw, int) else start
            text = "\n".join(lines[start - 1 : end])
            found.append(
                Symbol(
                    qualname=".".join([*self.stack, name]),
                    name=name,
                    kind=kind,
                    start_line=start,
                    end_line=end,
                    source=text,
                )
            )

    Visitor().visit(tree)
    return tuple(found)


def _body_key(symbol: Symbol) -> str:
    lines = symbol.source.splitlines()
    if not lines:
        return ""
    lines[0] = re.sub(r"\b(def|class)\s+\w+", r"\1 NAME", lines[0], count=1)
    return "\n".join(lines)


def pair_renames(
    removed: tuple[Symbol, ...], added: tuple[Symbol, ...]
) -> tuple[tuple[str, str], ...]:
    candidates: list[tuple[float, str, str]] = []
    for removed_symbol in removed:
        for added_symbol in added:
            if removed_symbol.kind != added_symbol.kind:
                continue
            score = difflib.SequenceMatcher(
                None, _body_key(removed_symbol), _body_key(added_symbol)
            ).ratio()
            if score >= _RENAME_SCORE:
                candidates.append((score, removed_symbol.qualname, added_symbol.qualname))
    candidates.sort(key=lambda item: item[0], reverse=True)
    used_old: set[str] = set()
    used_new: set[str] = set()
    pairs: list[tuple[str, str]] = []
    for _score, old_name, new_name in candidates:
        if old_name in used_old or new_name in used_new:
            continue
        used_old.add(old_name)
        used_new.add(new_name)
        pairs.append((old_name, new_name))
    return tuple(pairs)


def classify_symbols(
    base_symbols: tuple[Symbol, ...],
    head_symbols: tuple[Symbol, ...],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[tuple[str, str], ...]]:
    base_map = {item.qualname: item for item in base_symbols}
    head_map = {item.qualname: item for item in head_symbols}
    added = tuple(name for name in head_map if name not in base_map)
    removed = tuple(name for name in base_map if name not in head_map)
    changed = tuple(
        name
        for name, head in head_map.items()
        if name in base_map and base_map[name].source != head.source
    )
    removed_syms = tuple(base_map[name] for name in removed)
    added_syms = tuple(head_map[name] for name in added)
    return changed, added, removed, pair_renames(removed_syms, added_syms)


def _is_python(path: str) -> bool:
    return path.endswith(".py")


def analyze_repo(
    repo: Path,
    base: str,
    head: str,
    *,
    title: str = "",
    body: str = "",
) -> DiffAnalysis:
    base_sha = rev_parse(repo, base)
    head_sha = rev_parse(repo, head)
    name_status = git(repo, "diff", "--name-status", "-M", base_sha, head_sha)
    files: list[FileChange] = []
    for status, path, old_path in parse_name_status(name_status):
        if not _is_python(path) and not (old_path and _is_python(old_path)):
            continue
        base_lookup = old_path if status in {"renamed", "copied", "deleted"} and old_path else path
        base_source = None if status == "added" else show_file(repo, base_sha, base_lookup)
        head_source = None if status == "deleted" else show_file(repo, head_sha, path)
        base_symbols = extract_symbols(base_source) if base_source is not None else ()
        head_symbols = extract_symbols(head_source) if head_source is not None else ()
        changed, added, removed, renamed = classify_symbols(base_symbols, head_symbols)
        if status == "added":
            added = tuple(sym.qualname for sym in head_symbols) or added
        if status == "deleted":
            removed = tuple(sym.qualname for sym in base_symbols) or removed
        files.append(
            FileChange(
                path=path,
                status=status,
                old_path=old_path,
                base_source=base_source,
                head_source=head_source,
                base_symbols=base_symbols,
                head_symbols=head_symbols,
                changed_qualnames=changed,
                added_qualnames=added,
                removed_qualnames=removed,
                renamed=renamed,
            )
        )
    return DiffAnalysis(
        repo=str(repo),
        base_sha=base_sha,
        head_sha=head_sha,
        title=title,
        body=body,
        files=tuple(files),
    )


def _clip(text: str, limit: int = 12000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n# ... truncated ..."


def render_analysis(analysis: DiffAnalysis) -> str:
    """Text form of the diff that both generators put in the prompt."""
    parts = [
        f"title: {analysis.title}",
        "body:",
        analysis.body.strip(),
        "",
        f"base: {analysis.base_sha}",
        f"head: {analysis.head_sha}",
    ]
    if not analysis.files:
        parts.append("no Python files changed")
        return "\n".join(parts)
    for item in analysis.files:
        parts.append("")
        parts.append(f"file: {item.path}")
        parts.append(f"status: {item.status}")
        if item.old_path:
            parts.append(f"old_path: {item.old_path}")
        parts.append(f"import_as: {import_name(item.path)}")
        parts.append("changed_symbols: " + ", ".join(item.changed_qualnames))
        parts.append("added_symbols: " + ", ".join(item.added_qualnames))
        parts.append("removed_symbols: " + ", ".join(item.removed_qualnames))
        if item.renamed:
            rendered = ", ".join(f"{old} -> {new}" for old, new in item.renamed)
            parts.append(f"renames: {rendered}")
        if item.base_source is not None:
            parts.append("parent_source:")
            parts.append(_clip(item.base_source))
        if item.head_source is not None:
            parts.append("head_source:")
            parts.append(_clip(item.head_source))
    return "\n".join(parts)
