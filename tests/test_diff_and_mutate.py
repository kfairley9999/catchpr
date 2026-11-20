from __future__ import annotations

from pathlib import Path

import pytest
from tests.support import commit, init_repo

from catchpr.diff import (
    analyze_repo,
    classify_symbols,
    extract_symbols,
    import_name,
    parse_changed_lines,
    parse_name_status,
    render_analysis,
)
from catchpr.mutate import MutantError, replace_symbol

BASE = """\
def apply_discount(price: int, rate_bps: int) -> int:
    return price - price * rate_bps // 10000


def untouched(value: int) -> int:
    return value
"""

HEAD = """\
def apply_discount(price: int, rate_bps: int) -> int:
    return price + price * rate_bps // 10000


def untouched(value: int) -> int:
    return value
"""


def test_parse_name_status_and_hunks() -> None:
    text = "M\told.py\nA\tnew.py\nD\tgone.py\nR100\told_name.py\tnew_name.py\nC080\ta.py\tb.py\n"
    rows = parse_name_status(text)
    assert rows[0] == ("modified", "old.py", None)
    assert rows[1] == ("added", "new.py", None)
    assert rows[2] == ("deleted", "gone.py", None)
    assert rows[3] == ("renamed", "new_name.py", "old_name.py")
    assert rows[4][0] == "copied"
    diff = """diff --git a/x.py b/x.py
@@ -1,2 +1,3 @@
+line
 context
-old
"""
    # With a real hunk header the parser records added lines.
    diff = """@@ -1,2 +10,2 @@
 context
+added
-removed
"""
    assert parse_changed_lines(diff) == {11}
    assert parse_changed_lines("no hunks here") == set()
    assert parse_name_status("\n\n") == []
    assert parse_name_status("X\tignored\n") == []


def test_extract_symbols_and_renames() -> None:
    source = "class Box:\n    def __init__(self) -> None:\n        self.n = 1\n\n    async def go(self) -> int:\n        return 1\n"
    symbols = extract_symbols(source)
    names = [item.qualname for item in symbols]
    assert names == ["Box", "Box.__init__", "Box.go"]
    assert extract_symbols("def broken(:\n") == ()
    changed, added, removed, renamed = classify_symbols(
        extract_symbols("def alpha(value: int) -> int:\n    return value + 1\n"),
        extract_symbols("def beta(value: int) -> int:\n    return value + 1\n"),
    )
    assert changed == ()
    assert added == ("beta",)
    assert removed == ("alpha",)
    assert renamed == (("alpha", "beta"),)


def test_import_name() -> None:
    assert import_name("billing.py") == "billing"
    assert import_name("pkg/mod.py") == "pkg.mod"
    assert import_name("pkg/__init__.py") == "pkg"


def test_replace_symbol_and_errors() -> None:
    updated = replace_symbol(
        BASE,
        "apply_discount",
        "def apply_discount(price: int, rate_bps: int) -> int:\n    return 0\n",
    )
    assert "return 0" in updated
    assert "def untouched" in updated
    nested = "class Box:\n    def size(self) -> int:\n        return 1\n"
    replaced = replace_symbol(nested, "Box.size", "def size(self) -> int:\n    return 2\n")
    assert "return 2" in replaced
    with pytest.raises(MutantError):
        replace_symbol("def broken(:\n", "x", "def x():\n    return 1\n")
    with pytest.raises(MutantError):
        replace_symbol(BASE, "missing", "def missing():\n    return 1\n")
    with pytest.raises(MutantError):
        replace_symbol(BASE, "apply_discount", "def apply_discount(:\n")


def test_analyze_repo_finds_changed_function(tmp_path: Path) -> None:
    init_repo(tmp_path)
    base = commit(tmp_path, {"billing.py": BASE, "notes.md": "hi\n"}, "base")
    head = commit(tmp_path, {"billing.py": HEAD}, "head")
    analysis = analyze_repo(tmp_path, base, head, title="Fix discount", body="Adjust pricing.")
    assert analysis.base_sha == base
    assert len(analysis.files) == 1
    change = analysis.files[0]
    assert change.path == "billing.py"
    assert "apply_discount" in change.changed_qualnames
    assert "untouched" not in change.changed_qualnames
    rendered = render_analysis(analysis)
    assert "phase" not in rendered
    assert "apply_discount" in rendered
    assert "Fix discount" in rendered
    assert "apply_discount" in analysis.changed_names()


def test_analyze_added_deleted_and_rename(tmp_path: Path) -> None:
    init_repo(tmp_path)
    base = commit(
        tmp_path, {"old_mod.py": "def keep(value: int) -> int:\n    return value\n"}, "base"
    )
    (tmp_path / "old_mod.py").unlink()
    (tmp_path / "new_mod.py").write_text(
        "def keep(value: int) -> int:\n    return value\n", encoding="utf-8"
    )
    (tmp_path / "fresh.py").write_text("def fresh() -> int:\n    return 1\n", encoding="utf-8")
    head = commit(tmp_path, {}, "head")
    # commit() writes nothing new if files dict is empty, but files are already on disk
    # and `git add -A` inside commit picks them up. We already called commit.
    analysis = analyze_repo(tmp_path, base, head, title="rename", body="")
    statuses = {item.path: item.status for item in analysis.files}
    assert "fresh.py" in statuses
    assert statuses["fresh.py"] == "added"
    renamed = [item for item in analysis.files if item.status == "renamed"]
    assert renamed
    assert renamed[0].renamed or renamed[0].changed_qualnames == ()
    empty = analyze_repo(tmp_path, head, head, title="", body="")
    assert "no Python files changed" in render_analysis(empty)
    assert empty.primary_location() == ("UNKNOWN", 1)
