"""Intent-aware and dodgy-diff catching-test generators.

Both follow the diff-aware workflows in Becker et al., FSE 2026
(arXiv:2601.22832). Intent-aware infers the diff's intent, lists risks,
materializes each risk as a mutant of the parent, and asks for a test that
kills that mutant. Dodgy-diff treats the diff itself as the mutant.
"""

from __future__ import annotations

import ast

from catchpr.diff import render_analysis
from catchpr.errors import ParseError
from catchpr.jsonutil import as_dict, as_list, as_str, parse_model_json
from catchpr.models import DiffAnalysis, GeneratedTest, Mutant
from catchpr.providers.base import Provider

_BANNED_MODULES = {"subprocess", "socket", "ctypes", "pty", "pickle", "multiprocessing"}

INTENT_SYSTEM = """You generate catching tests for one pull request.
A catching test must pass on the parent revision and fail on a mutant that
implements a plausible mistake while trying to carry out the diff's intent.
Respond with one JSON object and no markdown.
Schema:
{"intent": string, "risks": [{"id": string, "description": string,
"mutant": {"path": string, "symbol": string, "replacement": string},
"test": string}]}
"replacement" is the full source of that symbol, edited so it contains the mistake.
"test" is a pytest module. It may import pytest and the modules named import_as.
Do not use network, randomness, clocks, or environment variables.
If there is no concrete risk, return {"intent": string, "risks": []}.
"""

MUTANT_TEST_SYSTEM = """You write one pytest module.
It must pass on the parent and fail on the mutant.
Respond with {"test": string} and no markdown.
Use only pytest and the standard library.
No network, randomness, clocks, or environment variables.
"""

DODGY_SYSTEM = """You generate tests that kill a dodgy diff.
Treat the child revision as a mutant of the parent. Do not trust the intent.
Respond with one JSON object and no markdown.
Schema:
{"tests": [{"id": string, "rationale": string, "test": string}]}
Each test is a pytest module that passes on the parent and fails if the child's
observable behavior is present.
No network, randomness, clocks, or environment variables.
If you cannot write one, return {"tests": []}.
"""


def validate_test_source(source: str) -> str | None:
    """Return a rejection reason, or None when the test is safe to execute."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return f"test does not parse: {exc.msg}"
    has_test = False
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
            "test_"
        ):
            has_test = True
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root in _BANNED_MODULES:
                    return f"banned import: {alias.name}"
        if isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".", 1)[0]
            if root in _BANNED_MODULES:
                return f"banned import: {node.module}"
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"eval", "exec", "__import__"}
        ):
            return f"banned call: {node.func.id}"
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "os"
            and node.func.attr in {"system", "popen"}
        ):
            return f"banned call: os.{node.func.attr}"
    if not has_test:
        return "no test_ function"
    return None


def generate_intent(analysis: DiffAnalysis, provider: Provider) -> list[GeneratedTest]:
    prompt = "phase: intent\n" + render_analysis(analysis)
    raw = provider.complete(prompt, system=INTENT_SYSTEM, temperature=0.0)
    try:
        data = parse_model_json(raw)
    except ParseError:
        return []
    risks = as_list(data.get("risks")) or []
    tests: list[GeneratedTest] = []
    for index, risk_value in enumerate(risks):
        risk = as_dict(risk_value)
        if risk is None:
            continue
        risk_id = as_str(risk.get("id")) or f"r{index + 1}"
        description = as_str(risk.get("description")) or ""
        mutant = _mutant(risk.get("mutant"))
        source = as_str(risk.get("test")) or ""
        if not source and mutant is not None:
            source = _request_mutant_test(analysis, provider, risk_id, description, mutant)
        built = _build("intent-aware", risk_id, description, source, mutant)
        if built is not None:
            tests.append(built)
    return tests


def generate_dodgy(analysis: DiffAnalysis, provider: Provider) -> list[GeneratedTest]:
    prompt = "phase: dodgy\n" + render_analysis(analysis)
    raw = provider.complete(prompt, system=DODGY_SYSTEM, temperature=0.0)
    try:
        data = parse_model_json(raw)
    except ParseError:
        return []
    rows = as_list(data.get("tests")) or []
    tests: list[GeneratedTest] = []
    for index, row_value in enumerate(rows):
        row = as_dict(row_value)
        if row is None:
            continue
        test_id = as_str(row.get("id")) or f"d{index + 1}"
        rationale = as_str(row.get("rationale")) or ""
        source = as_str(row.get("test")) or ""
        built = _build("dodgy-diff", test_id, rationale, source, None)
        if built is not None:
            tests.append(built)
    return tests


def _request_mutant_test(
    analysis: DiffAnalysis,
    provider: Provider,
    risk_id: str,
    description: str,
    mutant: Mutant,
) -> str:
    prompt = "\n".join(
        [
            "phase: mutant-test",
            f"risk_id: {risk_id}",
            f"risk: {description}",
            f"mutant_path: {mutant.path}",
            f"mutant_symbol: {mutant.symbol}",
            "mutant_replacement:",
            mutant.replacement,
            "",
            render_analysis(analysis),
        ]
    )
    raw = provider.complete(prompt, system=MUTANT_TEST_SYSTEM, temperature=0.0)
    try:
        data = parse_model_json(raw)
    except ParseError:
        return ""
    return as_str(data.get("test")) or ""


def _mutant(value: object) -> Mutant | None:
    data = as_dict(value)
    if data is None:
        return None
    path = as_str(data.get("path"))
    symbol = as_str(data.get("symbol"))
    replacement = as_str(data.get("replacement"))
    if not path or not symbol or not replacement:
        return None
    return Mutant(path=path, symbol=symbol, replacement=replacement)


def _build(
    workflow: str,
    test_id: str,
    rationale: str,
    source: str,
    mutant: Mutant | None,
) -> GeneratedTest | None:
    if not source.strip():
        return None
    return GeneratedTest(
        workflow=workflow,
        test_id=test_id,
        rationale=rationale,
        source=source if source.endswith("\n") else source + "\n",
        mutant=mutant,
        target_path=mutant.path if mutant else None,
    )
