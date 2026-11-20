"""Forty fixture libraries. Twenty seed a behavior bug. Twenty do not.

Recorded tests and recorded read-only reviews are fixtures. They stand in for
a model when the benchmark runs in mock mode. The probes are the ground truth
used to check that a "bug" case really changes behavior and a "refactor" case
does not. They are not the tests catchpr runs.
"""

from __future__ import annotations

import types
from dataclasses import dataclass


@dataclass(frozen=True)
class Case:
    id: str
    kind: str
    title: str
    body: str
    filename: str
    symbol: str
    base: str
    head: str
    probes: tuple[str, ...]
    intent_test: str
    mutant: str
    baseline: str
    summary: str
    dodgy: tuple[tuple[str, str, str], ...] = ()

    def module_name(self) -> str:
        return self.filename[: -len(".py")] if self.filename.endswith(".py") else self.filename


def _load(source: str, filename: str) -> types.ModuleType:
    name = filename.removesuffix(".py")
    module = types.ModuleType(name)
    module.__name__ = name
    exec(compile(source, filename, "exec"), module.__dict__)
    return module


def probe_holds(case: Case, source: str) -> bool:
    module = _load(source, case.filename)
    try:
        for probe in case.probes:
            exec(probe, {"mod": module})
    except Exception:
        return False
    return True


def _t(module: str, symbol: str, body: str) -> str:
    return f"from {module} import {symbol}\n\n{body if body.endswith(chr(10)) else body + chr(10)}"


CASES: tuple[Case, ...] = (
    Case(
        id="b01_trim_invoice",
        kind="bug",
        title="Drop the note column from invoice exports",
        body="Exports should keep the id and the amount. The trailing note is not part of the feed.",
        filename="invoice.py",
        symbol="trim_invoice",
        base="def trim_invoice(row: list[str]) -> list[str]:\n    return row[:-1]\n",
        head="def trim_invoice(row: list[str]) -> list[str]:\n    return row[:-2]\n",
        probes=('assert mod.trim_invoice(["id", "10.00", "note"]) == ["id", "10.00"]',),
        intent_test=_t(
            "invoice",
            "trim_invoice",
            'def test_trim_keeps_amount():\n    assert trim_invoice(["id", "10.00", "note"]) == ["id", "10.00"]\n',
        ),
        mutant="def trim_invoice(row: list[str]) -> list[str]:\n    return row[:-2]\n",
        baseline="bug",
        summary="The slice drops the amount as well as the note.",
    ),
    Case(
        id="b02_discount_sign",
        kind="bug",
        title="Apply the promotional discount",
        body="Subtract the basis-point discount from the price.",
        filename="pricing.py",
        symbol="apply_discount",
        base=(
            "def apply_discount(price: int, rate_bps: int) -> int:\n"
            "    return price - price * rate_bps // 10000\n"
        ),
        head=(
            "def apply_discount(price: int, rate_bps: int) -> int:\n"
            "    return price + price * rate_bps // 10000\n"
        ),
        probes=("assert mod.apply_discount(10000, 250) == 9750",),
        intent_test=_t(
            "pricing",
            "apply_discount",
            "def test_discount_reduces_price():\n    assert apply_discount(10000, 250) == 9750\n",
        ),
        mutant=(
            "def apply_discount(price: int, rate_bps: int) -> int:\n"
            "    return price + price * rate_bps // 10000\n"
        ),
        baseline="bug",
        summary="The discount is added to the price instead of subtracted.",
    ),
    Case(
        id="b03_min_balance",
        kind="bug",
        title="Clarify account eligibility",
        body="Accounts at or above the minimum balance stay eligible.",
        filename="accounts.py",
        symbol="eligible",
        base="def eligible(balance: int) -> bool:\n    return balance >= 100\n",
        head="def eligible(balance: int) -> bool:\n    return balance < 100\n",
        probes=("assert mod.eligible(150) is True", "assert mod.eligible(50) is False"),
        intent_test=_t(
            "accounts",
            "eligible",
            "def test_funded_account_is_eligible():\n    assert eligible(150) is True\n",
        ),
        mutant="def eligible(balance: int) -> bool:\n    return balance < 100\n",
        baseline="clean",
        summary="Looks like a boolean cleanup.",
    ),
    Case(
        id="b04_greeting_swap",
        kind="bug",
        title="Format the welcome line",
        body="Greet the member by given name, then family name.",
        filename="welcome.py",
        symbol="greeting",
        base=(
            'def greeting(given: str, family: str) -> str:\n    return f"Hello {given} {family}"\n'
        ),
        head=(
            'def greeting(given: str, family: str) -> str:\n    return f"Hello {family} {given}"\n'
        ),
        probes=('assert mod.greeting("Ada", "Lovelace") == "Hello Ada Lovelace"',),
        intent_test=_t(
            "welcome",
            "greeting",
            'def test_greeting_order():\n    assert greeting("Ada", "Lovelace") == "Hello Ada Lovelace"\n',
        ),
        mutant=(
            'def greeting(given: str, family: str) -> str:\n    return f"Hello {family} {given}"\n'
        ),
        baseline="bug",
        summary="The given name and family name are swapped.",
    ),
    Case(
        id="b05_senior_cutoff",
        kind="bug",
        title="Senior fare at 65",
        body="Riders aged 65 and over get the senior fare. 64 does not.",
        filename="fares.py",
        symbol="senior_fare",
        base="def senior_fare(age: int) -> bool:\n    return age >= 65\n",
        head="def senior_fare(age: int) -> bool:\n    return age > 65\n",
        probes=("assert mod.senior_fare(65) is True",),
        intent_test=_t(
            "fares",
            "senior_fare",
            "def test_age_65_is_senior():\n    assert senior_fare(65) is True\n",
        ),
        mutant="def senior_fare(age: int) -> bool:\n    return age > 65\n",
        baseline="bug",
        summary="A 65-year-old no longer qualifies.",
    ),
    Case(
        id="b06_join_sep",
        kind="bug",
        title="Share the field joiner",
        body="Move the joiner next to the exporter. Callers that omit the separator stay on commas.",
        filename="fields.py",
        symbol="join_fields",
        base=(
            "def join_fields(fields: list[str], sep: str = ',') -> str:\n    return sep.join(fields)\n"
        ),
        head=(
            "def join_fields(fields: list[str], sep: str = '|') -> str:\n    return sep.join(fields)\n"
        ),
        probes=('assert mod.join_fields(["a", "b"]) == "a,b"',),
        intent_test=_t(
            "fields",
            "join_fields",
            'def test_default_separator_is_comma():\n    assert join_fields(["a", "b"]) == "a,b"\n',
        ),
        mutant=(
            "def join_fields(fields: list[str], sep: str = '|') -> str:\n    return sep.join(fields)\n"
        ),
        baseline="bug",
        summary="The default separator changed from comma to pipe.",
    ),
    Case(
        id="b07_leaderboard",
        kind="bug",
        title="Sort the leaderboard",
        body="Highest score first.",
        filename="board.py",
        symbol="leaderboard",
        base=(
            "def leaderboard(scores: list[int]) -> list[int]:\n    return sorted(scores, reverse=True)\n"
        ),
        head="def leaderboard(scores: list[int]) -> list[int]:\n    return sorted(scores)\n",
        probes=("assert mod.leaderboard([1, 3, 2]) == [3, 2, 1]",),
        intent_test=_t(
            "board",
            "leaderboard",
            "def test_highest_score_first():\n    assert leaderboard([1, 3, 2]) == [3, 2, 1]\n",
        ),
        mutant="def leaderboard(scores: list[int]) -> list[int]:\n    return sorted(scores)\n",
        baseline="bug",
        summary="Scores are sorted ascending.",
    ),
    Case(
        id="b08_rank_mutates",
        kind="bug",
        title="Rank scores for the report",
        body="Return scores from low to high for the report renderer.",
        filename="rank.py",
        symbol="rank_scores",
        base="def rank_scores(scores: list[int]) -> list[int]:\n    return sorted(scores)\n",
        head=(
            "def rank_scores(scores: list[int]) -> list[int]:\n    scores.sort()\n    return scores\n"
        ),
        probes=(
            "original = [3, 1, 2]\n"
            "assert mod.rank_scores(original) == [1, 2, 3]\n"
            "assert original == [3, 1, 2]",
        ),
        intent_test=_t(
            "rank",
            "rank_scores",
            "def test_rank_does_not_mutate_caller():\n"
            "    original = [3, 1, 2]\n"
            "    assert rank_scores(original) == [1, 2, 3]\n"
            "    assert original == [3, 1, 2]\n",
        ),
        mutant=(
            "def rank_scores(scores: list[int]) -> list[int]:\n    scores.sort()\n    return scores\n"
        ),
        baseline="clean",
        summary="Sorting cleanup, same order.",
    ),
    Case(
        id="b09_colour_key",
        kind="bug",
        title="Read the pixel channel",
        body="The feed stores the channel under colour.",
        filename="pixels.py",
        symbol="channel",
        base='def channel(pixel: dict[str, int]) -> int:\n    return pixel["colour"]\n',
        head='def channel(pixel: dict[str, int]) -> int:\n    return pixel["color"]\n',
        probes=('assert mod.channel({"colour": 7}) == 7',),
        intent_test=_t(
            "pixels",
            "channel",
            'def test_colour_key():\n    assert channel({"colour": 7}) == 7\n',
        ),
        mutant='def channel(pixel: dict[str, int]) -> int:\n    return pixel["color"]\n',
        baseline="bug",
        summary="The lookup key no longer matches the payload.",
    ),
    Case(
        id="b10_tally_start",
        kind="bug",
        title="Tally the ballot",
        body="Count the marks. An empty ballot is zero.",
        filename="ballot.py",
        symbol="tally",
        base=(
            "def tally(marks: list[int]) -> int:\n"
            "    total = 0\n"
            "    for mark in marks:\n"
            "        total += mark\n"
            "    return total\n"
        ),
        head=(
            "def tally(marks: list[int]) -> int:\n"
            "    total = 1\n"
            "    for mark in marks:\n"
            "        total += mark\n"
            "    return total\n"
        ),
        probes=("assert mod.tally([2, 3]) == 5", "assert mod.tally([]) == 0"),
        intent_test=_t(
            "ballot",
            "tally",
            "def test_tally_starts_at_zero():\n    assert tally([2, 3]) == 5\n",
        ),
        mutant=(
            "def tally(marks: list[int]) -> int:\n"
            "    total = 1\n"
            "    for mark in marks:\n"
            "        total += mark\n"
            "    return total\n"
        ),
        baseline="bug",
        summary="The accumulator starts at 1.",
    ),
    Case(
        id="b11_page_window",
        kind="bug",
        title="Page the audit rows",
        body="Each page returns `size` rows.",
        filename="pager.py",
        symbol="page",
        base=(
            "def page(items: list[int], index: int, size: int) -> list[int]:\n"
            "    start = index * size\n"
            "    return items[start : start + size]\n"
        ),
        head=(
            "def page(items: list[int], index: int, size: int) -> list[int]:\n"
            "    start = index * size\n"
            "    return items[start : start + size - 1]\n"
        ),
        probes=("assert mod.page([1, 2, 3, 4], 0, 2) == [1, 2]",),
        intent_test=_t(
            "pager",
            "page",
            "def test_first_item_still_present():\n    assert page([1, 2, 3, 4], 0, 2)[0] == 1\n",
        ),
        mutant=(
            "def page(items: list[int], index: int, size: int) -> list[int]:\n"
            "    start = index * size\n"
            "    return items[start : start + size - 1]\n"
        ),
        baseline="bug",
        summary="The page window is short by one.",
    ),
    Case(
        id="b12_round_div",
        kind="bug",
        title="Split a tip evenly",
        body="When the remainder is exactly half, round half up.",
        filename="tips.py",
        symbol="round_div",
        base=(
            "def round_div(n: int, d: int) -> int:\n"
            "    q, r = divmod(n, d)\n"
            "    if r * 2 >= d:\n"
            "        return q + 1\n"
            "    return q\n"
        ),
        head=(
            "def round_div(n: int, d: int) -> int:\n"
            "    q, r = divmod(n, d)\n"
            "    if r * 2 > d:\n"
            "        return q + 1\n"
            "    if r * 2 == d and q % 2 == 1:\n"
            "        return q + 1\n"
            "    return q\n"
        ),
        probes=("assert mod.round_div(5, 2) == 3",),
        intent_test=_t(
            "tips",
            "round_div",
            "def test_half_rounds_up():\n    assert round_div(5, 2) == 3\n",
        ),
        mutant=(
            "def round_div(n: int, d: int) -> int:\n"
            "    q, r = divmod(n, d)\n"
            "    if r * 2 > d:\n"
            "        return q + 1\n"
            "    if r * 2 == d and q % 2 == 1:\n"
            "        return q + 1\n"
            "    return q\n"
        ),
        baseline="clean",
        summary="Rounding helper, same for the examples I tried.",
    ),
    Case(
        id="b13_trim_marker",
        kind="bug",
        title="Strip one emphasis marker",
        body="Remove a single leading and trailing asterisk, not a run of them.",
        filename="markers.py",
        symbol="trim_marker",
        base=(
            "def trim_marker(token: str) -> str:\n"
            "    if token.startswith('*'):\n"
            "        token = token[1:]\n"
            "    if token.endswith('*'):\n"
            "        token = token[:-1]\n"
            "    return token\n"
        ),
        head="def trim_marker(token: str) -> str:\n    return token.strip('*')\n",
        probes=('assert mod.trim_marker("**note**") == "*note*"',),
        intent_test=_t(
            "markers",
            "trim_marker",
            'def test_one_marker_each_side():\n    assert trim_marker("**note**") == "*note*"\n',
        ),
        mutant="def trim_marker(token: str) -> str:\n    return token.strip('*')\n",
        baseline="bug",
        summary="str.strip removes every asterisk, not one on each side.",
    ),
    Case(
        id="b14_order_id",
        kind="bug",
        title="Normalize order ids",
        body="Trim surrounding space. Preserve case; ids are case-sensitive.",
        filename="orders.py",
        symbol="normalize_id",
        base="def normalize_id(order_id: str) -> str:\n    return order_id.strip()\n",
        head="def normalize_id(order_id: str) -> str:\n    return order_id.strip().lower()\n",
        probes=('assert mod.normalize_id(" Ab12 ") == "Ab12"',),
        intent_test=_t(
            "orders",
            "normalize_id",
            'def test_already_lower_is_trimmed():\n    assert normalize_id("  ab12 ") == "ab12"\n',
        ),
        mutant="def normalize_id(order_id: str) -> str:\n    return order_id.strip().lower()\n",
        baseline="bug",
        summary="Case is folded, so distinct ids can collide.",
    ),
    Case(
        id="b15_parse_qty",
        kind="bug",
        title="Parse a quantity field",
        body="Negative quantities are rejected. Callers handle ValueError.",
        filename="qty.py",
        symbol="parse_qty",
        base=(
            "def parse_qty(raw: str) -> int:\n"
            "    value = int(raw)\n"
            "    if value < 0:\n"
            "        raise ValueError('quantity must be >= 0')\n"
            "    return value\n"
        ),
        head=(
            "def parse_qty(raw: str) -> int:\n"
            "    try:\n"
            "        value = int(raw)\n"
            "    except ValueError:\n"
            "        return 0\n"
            "    if value < 0:\n"
            "        return 0\n"
            "    return value\n"
        ),
        probes=(
            "try:\n"
            "    mod.parse_qty('-3')\n"
            "except ValueError:\n"
            "    pass\n"
            "else:\n"
            "    raise AssertionError('expected ValueError')",
        ),
        intent_test=_t(
            "qty",
            "parse_qty",
            "import pytest\n\n"
            "def test_negative_qty_raises():\n"
            "    with pytest.raises(ValueError):\n"
            "        parse_qty('-3')\n",
        ),
        mutant=(
            "def parse_qty(raw: str) -> int:\n"
            "    try:\n"
            "        value = int(raw)\n"
            "    except ValueError:\n"
            "        return 0\n"
            "    if value < 0:\n"
            "        return 0\n"
            "    return value\n"
        ),
        baseline="clean",
        summary="Invalid quantities become a default. Seems deliberate.",
    ),
    Case(
        id="b16_keep_evens",
        kind="bug",
        title="Keep the even samples",
        body="The chart uses the even samples only.",
        filename="samples.py",
        symbol="evens",
        base="def evens(nums: list[int]) -> list[int]:\n    return [n for n in nums if n % 2 == 0]\n",
        head="def evens(nums: list[int]) -> list[int]:\n    return [n for n in nums if n % 2 == 1]\n",
        probes=("assert mod.evens([1, 2, 3, 4]) == [2, 4]",),
        intent_test=_t(
            "samples",
            "evens",
            "def test_empty_stays_empty():\n    assert evens([]) == []\n",
        ),
        mutant="def evens(nums: list[int]) -> list[int]:\n    return [n for n in nums if n % 2 == 1]\n",
        baseline="bug",
        summary="The predicate keeps odds.",
    ),
    Case(
        id="b17_inclusive_span",
        kind="bug",
        title="List the days in the stay",
        body="A stay of n nights includes night n. range should run through n.",
        filename="stays.py",
        symbol="nights",
        base="def nights(count: int) -> list[int]:\n    return list(range(1, count + 1))\n",
        head="def nights(count: int) -> list[int]:\n    return list(range(1, count))\n",
        probes=("assert mod.nights(3) == [1, 2, 3]",),
        intent_test=_t(
            "stays",
            "nights",
            "def test_three_night_stay():\n    assert nights(3) == [1, 2, 3]\n",
        ),
        mutant="def nights(count: int) -> list[int]:\n    return list(range(1, count))\n",
        baseline="clean",
        summary="Off-by-one is easy to miss in a range cleanup.",
    ),
    Case(
        id="b18_join_names",
        kind="bug",
        title="Render the guest list",
        body="Separate guest names with a comma and a space.",
        filename="guests.py",
        symbol="guest_list",
        base='def guest_list(names: list[str]) -> str:\n    return ", ".join(names)\n',
        head='def guest_list(names: list[str]) -> str:\n    return "".join(names)\n',
        probes=('assert mod.guest_list(["Ada", "Grace"]) == "Ada, Grace"',),
        intent_test=_t(
            "guests",
            "guest_list",
            'def test_names_are_separated():\n    assert guest_list(["Ada", "Grace"]) == "Ada, Grace"\n',
        ),
        mutant='def guest_list(names: list[str]) -> str:\n    return "".join(names)\n',
        baseline="bug",
        summary="Names are concatenated with no separator.",
    ),
    Case(
        id="b19_percent_of",
        kind="bug",
        title="Report the share as a percent",
        body="Return a percentage in the range 0 to 100, not a fraction.",
        filename="shares.py",
        symbol="percent_of",
        base=("def percent_of(part: int, total: int) -> int:\n    return part * 100 // total\n"),
        head="def percent_of(part: int, total: int) -> int:\n    return part // total\n",
        probes=("assert mod.percent_of(1, 4) == 25",),
        intent_test=_t(
            "shares",
            "percent_of",
            "def test_quarter_is_25_percent():\n    assert percent_of(1, 4) == 25\n",
        ),
        mutant="def percent_of(part: int, total: int) -> int:\n    return part // total\n",
        baseline="clean",
        summary="Integer division cleanup.",
    ),
    Case(
        id="b20_tag_counts",
        kind="bug",
        title="Count tags",
        body="Each tag contributes one to its count.",
        filename="tags.py",
        symbol="count_tags",
        base=(
            "def count_tags(tags: list[str]) -> dict[str, int]:\n"
            "    counts: dict[str, int] = {}\n"
            "    for tag in tags:\n"
            "        counts[tag] = counts.get(tag, 0) + 1\n"
            "    return counts\n"
        ),
        head=(
            "def count_tags(tags: list[str]) -> dict[str, int]:\n"
            "    counts: dict[str, int] = {}\n"
            "    for tag in tags:\n"
            "        counts[tag] = counts.get(tag, 0) + 1\n"
            "        counts[tag] = counts.get(tag, 0) + 1\n"
            "    return counts\n"
        ),
        probes=('assert mod.count_tags(["a", "a"]) == {"a": 2}',),
        intent_test=_t(
            "tags",
            "count_tags",
            "def test_empty_has_no_counts():\n    assert count_tags([]) == {}\n",
        ),
        mutant=(
            "def count_tags(tags: list[str]) -> dict[str, int]:\n"
            "    counts: dict[str, int] = {}\n"
            "    for tag in tags:\n"
            "        counts[tag] = counts.get(tag, 0) + 1\n"
            "        counts[tag] = counts.get(tag, 0) + 1\n"
            "    return counts\n"
        ),
        baseline="bug",
        summary="Every tag is counted twice.",
    ),
)


def _refactor(
    id: str,
    title: str,
    body: str,
    filename: str,
    symbol: str,
    base: str,
    head: str,
    probes: tuple[str, ...],
    test: str,
    baseline: str,
    summary: str,
    dodgy: tuple[tuple[str, str, str], ...] = (),
) -> Case:
    return Case(
        id=id,
        kind="refactor",
        title=title,
        body=body,
        filename=filename,
        symbol=symbol,
        base=base,
        head=head,
        probes=probes,
        intent_test=test,
        mutant=base[base.index(f"def {symbol}") :],
        baseline=baseline,
        summary=summary,
        dodgy=dodgy,
    )


_REFACTORS: tuple[Case, ...] = (
    _refactor(
        "r01_rename_locals",
        "Rename the running total",
        "No behavior change. The local names were unclear.",
        "totals.py",
        "total",
        "def total(values: list[int]) -> int:\n    acc = 0\n    for value in values:\n        acc += value\n    return acc\n",
        "def total(values: list[int]) -> int:\n    running = 0\n    for item in values:\n        running += item\n    return running\n",
        ("assert mod.total([1, 2, 3]) == 6",),
        _t("totals", "total", "def test_total():\n    assert total([1, 2, 3]) == 6\n"),
        "clean",
        "Local names only.",
    ),
    _refactor(
        "r02_extract_helper",
        "Extract the addition",
        "Pull the addition into a helper. The total is unchanged.",
        "totals2.py",
        "total",
        "def total(values: list[int]) -> int:\n    acc = 0\n    for value in values:\n        acc += value\n    return acc\n",
        "def _add(acc: int, value: int) -> int:\n    return acc + value\n\ndef total(values: list[int]) -> int:\n    acc = 0\n    for value in values:\n        acc = _add(acc, value)\n    return acc\n",
        ("assert mod.total([4, 1]) == 5",),
        _t("totals2", "total", "def test_total():\n    assert total([4, 1]) == 5\n"),
        "bug",
        "New helper might drop a value.",
    ),
    _refactor(
        "r03_comprehension",
        "Use a comprehension",
        "Same squares, written as a comprehension.",
        "squares.py",
        "squares",
        "def squares(nums: list[int]) -> list[int]:\n    out = []\n    for num in nums:\n        out.append(num * num)\n    return out\n",
        "def squares(nums: list[int]) -> list[int]:\n    return [num * num for num in nums]\n",
        ("assert mod.squares([2, 3]) == [4, 9]",),
        _t("squares", "squares", "def test_squares():\n    assert squares([2, 3]) == [4, 9]\n"),
        "clean",
        "Equivalent comprehension.",
    ),
    _refactor(
        "r04_docstring",
        "Document clamp",
        "Docstring only.",
        "clamp.py",
        "clamp",
        "def clamp(value: int, low: int, high: int) -> int:\n    if value < low:\n        return low\n    if value > high:\n        return high\n    return value\n",
        'def clamp(value: int, low: int, high: int) -> int:\n    """Clamp value into [low, high]."""\n    if value < low:\n        return low\n    if value > high:\n        return high\n    return value\n',
        ("assert mod.clamp(-1, 0, 5) == 0", "assert mod.clamp(3, 0, 5) == 3"),
        _t(
            "clamp",
            "clamp",
            "def test_clamp():\n    assert clamp(-1, 0, 5) == 0\n    assert clamp(3, 0, 5) == 3\n",
        ),
        "clean",
        "Documentation only.",
    ),
    _refactor(
        "r05_reorder",
        "Reorder independent assignments",
        "The two assignments do not depend on each other.",
        "pair.py",
        "pair",
        "def pair(x: int, y: int) -> tuple[int, int]:\n    left = x + 1\n    right = y + 2\n    return left, right\n",
        "def pair(x: int, y: int) -> tuple[int, int]:\n    right = y + 2\n    left = x + 1\n    return left, right\n",
        ("assert mod.pair(3, 4) == (4, 6)",),
        _t("pair", "pair", "def test_pair():\n    assert pair(3, 4) == (4, 6)\n"),
        "bug",
        "Reordering might have swapped the tuple.",
    ),
    _refactor(
        "r06_ternary",
        "Write the sign as a conditional expression",
        "Same sign function.",
        "signs.py",
        "sign",
        "def sign(value: int) -> int:\n    if value < 0:\n        return -1\n    if value > 0:\n        return 1\n    return 0\n",
        "def sign(value: int) -> int:\n    return -1 if value < 0 else 1 if value > 0 else 0\n",
        ("assert mod.sign(-4) == -1", "assert mod.sign(0) == 0", "assert mod.sign(2) == 1"),
        _t(
            "signs",
            "sign",
            "def test_sign():\n    assert sign(-4) == -1\n    assert sign(0) == 0\n",
        ),
        "clean",
        "Same branches.",
    ),
    _refactor(
        "r07_rename_private",
        "Rename the private normalizer",
        "The public entry point is unchanged.",
        "labels.py",
        "label",
        "def _norm(text: str) -> str:\n    return text.strip()\n\ndef label(text: str) -> str:\n    return _norm(text)\n",
        "def _normalize(text: str) -> str:\n    return text.strip()\n\ndef label(text: str) -> str:\n    return _normalize(text)\n",
        ('assert mod.label("  hi ") == "hi"',),
        _t("labels", "label", 'def test_label():\n    assert label("  hi ") == "hi"\n'),
        "bug",
        "A renamed helper can break callers.",
    ),
    _refactor(
        "r08_dict_dispatch",
        "Dispatch on a table",
        "The two operations are the same.",
        "ops.py",
        "apply_op",
        "def apply_op(op: str, value: int) -> int:\n    if op == 'inc':\n        return value + 1\n    if op == 'dbl':\n        return value * 2\n    raise ValueError(op)\n",
        "def apply_op(op: str, value: int) -> int:\n    table = {'inc': value + 1, 'dbl': value * 2}\n    if op not in table:\n        raise ValueError(op)\n    return table[op]\n",
        ("assert mod.apply_op('inc', 3) == 4", "assert mod.apply_op('dbl', 3) == 6"),
        _t(
            "ops",
            "apply_op",
            "def test_ops():\n    assert apply_op('inc', 3) == 4\n    assert apply_op('dbl', 3) == 6\n",
        ),
        "bug",
        "The table might miss an operation.",
    ),
    _refactor(
        "r09_inline_constant",
        "Inline the tax rate",
        "The rate was a local constant. The arithmetic is the same.",
        "tax.py",
        "tax_on",
        "def tax_on(cents: int) -> int:\n    rate = 5\n    return cents * rate // 100\n",
        "def tax_on(cents: int) -> int:\n    return cents * 5 // 100\n",
        ("assert mod.tax_on(200) == 10",),
        _t("tax", "tax_on", "def test_tax():\n    assert tax_on(200) == 10\n"),
        "clean",
        "Constant inlined.",
    ),
    _refactor(
        "r10_fstring",
        "Use an f-string",
        "Same message.",
        "messages.py",
        "message",
        'def message(name: str, n: int) -> str:\n    return name + " has " + str(n)\n',
        'def message(name: str, n: int) -> str:\n    return f"{name} has {n}"\n',
        ('assert mod.message("Ada", 2) == "Ada has 2"',),
        _t(
            "messages",
            "message",
            'def test_message():\n    assert message("Ada", 2) == "Ada has 2"\n',
        ),
        "clean",
        "Formatting only.",
    ),
    _refactor(
        "r11_builtin_sum",
        "Call the builtin sum",
        "Same total.",
        "sums.py",
        "total",
        "def total(values: list[int]) -> int:\n    acc = 0\n    for value in values:\n        acc += value\n    return acc\n",
        "def total(values: list[int]) -> int:\n    return sum(values)\n",
        ("assert mod.total([1, 2, 3]) == 6", "assert mod.total([]) == 0"),
        _t("sums", "total", "def test_total():\n    assert total([1, 2, 3]) == 6\n"),
        "clean",
        "Builtin sum.",
    ),
    _refactor(
        "r12_bool_simplify",
        "Simplify the boolean",
        "not (a and b) is unchanged via De Morgan in the other form? Keep it equivalent: return not flag.",
        "flags.py",
        "disabled",
        "def disabled(flag: bool) -> bool:\n    if flag:\n        return False\n    return True\n",
        "def disabled(flag: bool) -> bool:\n    return not flag\n",
        ("assert mod.disabled(True) is False", "assert mod.disabled(False) is True"),
        _t("flags", "disabled", "def test_disabled():\n    assert disabled(True) is False\n"),
        "clean",
        "Equivalent boolean.",
    ),
    _refactor(
        "r13_split_function",
        "Split validation from formatting",
        "The public function still returns the same string.",
        "codes.py",
        "format_code",
        "def format_code(raw: str) -> str:\n    cleaned = raw.strip()\n    if not cleaned:\n        raise ValueError('empty')\n    return cleaned.upper()\n",
        "def _clean(raw: str) -> str:\n    cleaned = raw.strip()\n    if not cleaned:\n        raise ValueError('empty')\n    return cleaned\n\ndef format_code(raw: str) -> str:\n    return _clean(raw).upper()\n",
        ('assert mod.format_code(" ab ") == "AB"',),
        _t(
            "codes",
            "format_code",
            'def test_format_code():\n    assert format_code(" ab ") == "AB"\n',
        ),
        "bug",
        "Splitting the function can drop the uppercasing.",
    ),
    _refactor(
        "r14_clamp_early",
        "Return early from clamp",
        "Same clamp, early returns already used. Rewrite the middle as a fallthrough that is identical.",
        "bounds.py",
        "bounds",
        "def bounds(value: int) -> str:\n    if value < 0:\n        return 'low'\n    elif value == 0:\n        return 'zero'\n    else:\n        return 'high'\n",
        "def bounds(value: int) -> str:\n    if value < 0:\n        return 'low'\n    if value == 0:\n        return 'zero'\n    return 'high'\n",
        (
            "assert mod.bounds(-1) == 'low'",
            "assert mod.bounds(0) == 'zero'",
            "assert mod.bounds(4) == 'high'",
        ),
        _t(
            "bounds",
            "bounds",
            "def test_bounds():\n    assert bounds(-1) == 'low'\n    assert bounds(0) == 'zero'\n    assert bounds(4) == 'high'\n",
        ),
        "clean",
        "elif flattened.",
    ),
    _refactor(
        "r15_early_continue",
        "Continue instead of nesting",
        "Skip non-positive numbers the same way.",
        "positives.py",
        "positives",
        "def positives(nums: list[int]) -> list[int]:\n    out = []\n    for num in nums:\n        if num > 0:\n            out.append(num)\n    return out\n",
        "def positives(nums: list[int]) -> list[int]:\n    out = []\n    for num in nums:\n        if num <= 0:\n            continue\n        out.append(num)\n    return out\n",
        ("assert mod.positives([-1, 2, 0, 3]) == [2, 3]",),
        _t(
            "positives",
            "positives",
            "def test_positives():\n    assert positives([-1, 2, 0, 3]) == [2, 3]\n",
        ),
        "clean",
        "Continue is the same filter.",
    ),
    _refactor(
        "r16_comments",
        "Explain the index math",
        "Comments only.",
        "index.py",
        "offset",
        "def offset(index: int, size: int) -> int:\n    return index * size\n",
        "def offset(index: int, size: int) -> int:\n    # Row-major offset into a page.\n    return index * size\n",
        ("assert mod.offset(2, 10) == 20",),
        _t("index", "offset", "def test_offset():\n    assert offset(2, 10) == 20\n"),
        "clean",
        "Comment only.",
    ),
    _refactor(
        "r17_whitespace",
        "Reformat the return",
        "Parentheses only.",
        "widths.py",
        "width",
        "def width(left: int, right: int) -> int:\n    return left + right\n",
        "def width(left: int, right: int) -> int:\n    return (left + right)\n",
        ("assert mod.width(2, 5) == 7",),
        _t("widths", "width", "def test_width():\n    assert width(2, 5) == 7\n"),
        "clean",
        "Parentheses only.",
    ),
    _refactor(
        "r18_loop_equiv",
        "Build the prefix with a loop",
        "Same characters, gathered in a loop instead of a slice.",
        "prefix.py",
        "prefix",
        "def prefix(text: str, count: int) -> str:\n    return text[:count]\n",
        "def prefix(text: str, count: int) -> str:\n    chars = []\n    for index, char in enumerate(text):\n        if index >= count:\n            break\n        chars.append(char)\n    return ''.join(chars)\n",
        ('assert mod.prefix("abcdef", 3) == "abc"',),
        _t("prefix", "prefix", 'def test_prefix():\n    assert prefix("abcdef", 3) == "abc"\n'),
        "bug",
        "The loop may keep one extra character.",
    ),
    _refactor(
        "r19_filtered_clock",
        "Leave the adder alone",
        "No behavior change. A generated test that calls random is not evidence.",
        "adder.py",
        "add",
        "def add(left: int, right: int) -> int:\n    return left + right\n",
        "def add(left: int, right: int) -> int:\n    return (left + right)\n",
        ("assert mod.add(2, 3) == 5",),
        _t("adder", "add", "def test_add():\n    assert add(2, 3) == 5\n"),
        "clean",
        "Parentheses only.",
        dodgy=(
            (
                "stable",
                "parent sum",
                _t("adder", "add", "def test_add():\n    assert add(2, 3) == 5\n"),
            ),
            (
                "noisy",
                "unrelated sample",
                "import random\n\ndef test_sample():\n    assert random.random() >= 0\n",
            ),
        ),
    ),
    _refactor(
        "r20_filtered_rename",
        "Rename the private formatter",
        "format_invoice is unchanged. _format_total was only an internal name.",
        "billing.py",
        "format_invoice",
        "def _format_total(cents: int) -> str:\n    return f'{cents / 100:.2f}'\n\ndef format_invoice(cents: int) -> str:\n    return _format_total(cents)\n",
        "def _render_total(cents: int) -> str:\n    return f'{cents / 100:.2f}'\n\ndef format_invoice(cents: int) -> str:\n    return _render_total(cents)\n",
        ('assert mod.format_invoice(250) == "2.50"',),
        _t(
            "billing",
            "format_invoice",
            'def test_invoice():\n    assert format_invoice(250) == "2.50"\n',
        ),
        "clean",
        "Private rename.",
        dodgy=(
            (
                "private",
                "old private name",
                "from billing import _format_total\n\n"
                "def test_private_total():\n"
                "    assert _format_total(250) == '2.50'\n",
            ),
        ),
    ),
)

CASES = CASES + _REFACTORS


def assert_catalog() -> None:
    bugs = [case for case in CASES if case.kind == "bug"]
    refactors = [case for case in CASES if case.kind == "refactor"]
    if len(CASES) != 40 or len(bugs) != 20 or len(refactors) != 20:
        raise AssertionError(f"expected 20/20, got {len(bugs)} bugs and {len(refactors)} refactors")
    ids = [case.id for case in CASES]
    if len(ids) != len(set(ids)):
        raise AssertionError("duplicate case id")
    for case in CASES:
        if not probe_holds(case, case.base):
            raise AssertionError(f"{case.id} probe failed on the base")
        head_ok = probe_holds(case, case.head)
        if case.kind == "bug" and head_ok:
            raise AssertionError(f"{case.id} does not change behavior")
        if case.kind == "refactor" and not head_ok:
            raise AssertionError(f"{case.id} refactor changes behavior")
