"""Apply a mutant by replacing one function or class in the parent source."""

from __future__ import annotations

import ast

from catchpr.errors import CatchprError


class MutantError(CatchprError):
    """The mutant could not be applied to the parent file."""


def _find(tree: ast.AST, qualname: str) -> ast.AST | None:
    wanted = qualname.split(".")

    class Finder(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []
            self.found: ast.AST | None = None

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self._check(node)
            self.stack.append(node.name)
            if self.found is None:
                self.generic_visit(node)
            self.stack.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._check(node)
            self.stack.append(node.name)
            if self.found is None:
                self.generic_visit(node)
            self.stack.pop()

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._check(node)
            self.stack.append(node.name)
            if self.found is None:
                self.generic_visit(node)
            self.stack.pop()

        def _check(self, node: ast.AST) -> None:
            name = getattr(node, "name", "")
            if [*self.stack, name] == wanted:
                self.found = node

    finder = Finder()
    finder.visit(tree)
    return finder.found


def _indent_of(source: str, lineno: int) -> str:
    lines = source.splitlines()
    if lineno < 1 or lineno > len(lines):
        return ""
    line = lines[lineno - 1]
    return line[: len(line) - len(line.lstrip(" \t"))]


def _reindent(replacement: str, indent: str) -> str:
    text = replacement.strip("\n")
    raw_lines = text.splitlines()
    nonempty = [line for line in raw_lines if line.strip()]
    if not nonempty:
        return ""
    common = min(len(line) - len(line.lstrip(" ")) for line in nonempty)
    trimmed = [(line[common:] if line.strip() else "") for line in raw_lines]
    indented = [(indent + line if line else indent.rstrip(" ")) for line in trimmed]
    return "\n".join(indented)


def replace_symbol(source: str, qualname: str, replacement: str) -> str:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise MutantError(f"parent file does not parse: {exc}") from exc
    node = _find(tree, qualname)
    if node is None:
        raise MutantError(f"symbol {qualname} was not found in the parent")
    start = int(getattr(node, "lineno", 0))
    end_raw = getattr(node, "end_lineno", None)
    end = int(end_raw) if isinstance(end_raw, int) else start
    if start < 1 or end < start:
        raise MutantError(f"symbol {qualname} has no source span")
    indent = _indent_of(source, start)
    rendered = _reindent(replacement, indent)
    if not rendered.endswith("\n"):
        rendered += "\n"
    lines = source.splitlines(keepends=True)
    prefix = "".join(lines[: start - 1])
    suffix = "".join(lines[end:])
    updated = prefix + rendered + suffix
    try:
        ast.parse(updated)
    except SyntaxError as exc:
        raise MutantError(f"mutant for {qualname} does not parse: {exc}") from exc
    return updated
