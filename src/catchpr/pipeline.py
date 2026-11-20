"""Wire diff analysis, both generators, the sandbox, and the assessors."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from catchpr.diff import analyze_repo
from catchpr.errors import ProviderError
from catchpr.filters.environment import environment_failure, environment_source_reason
from catchpr.filters.imports import import_failure
from catchpr.filters.intended import intended_change
from catchpr.filters.judge import judge_catch
from catchpr.filters.symbols import symbol_failure
from catchpr.generate import generate_dodgy, generate_intent, validate_test_source
from catchpr.models import (
    Catch,
    DiffAnalysis,
    GeneratedTest,
    ProcResult,
    Rejection,
    Report,
)
from catchpr.providers.base import Provider
from catchpr.report import reproduce_command
from catchpr.runner import Sandbox


@dataclass(frozen=True)
class RunConfig:
    repo: Path
    base: str
    head: str
    title: str = ""
    body: str = ""
    repeats: int = 3
    timeout: float = 20.0
    use_judge: bool = False
    max_catches: int = 5
    block_network: bool = True


def run_pipeline(config: RunConfig, provider: Provider) -> Report:
    if config.repeats < 1:
        raise ValueError("repeats must be >= 1")
    analysis = analyze_repo(
        config.repo,
        config.base,
        config.head,
        title=config.title,
        body=config.body,
    )
    report = Report(
        base_sha=analysis.base_sha,
        head_sha=analysis.head_sha,
        title=config.title,
        body=config.body,
    )
    tests = _generate(analysis, provider, report)
    unique = _prepare(tests, report)
    if not unique:
        return report
    with Sandbox(
        config.repo,
        analysis.base_sha,
        analysis.head_sha,
        timeout=config.timeout,
        block_network=config.block_network,
    ) as sandbox:
        for test in unique:
            catch, rejection = _confirm(sandbox, test, config.repeats)
            if catch is None:
                if rejection is not None:
                    report.rejections.append(rejection)
                continue
            path, line = _location(test, analysis)
            catch.location_path = path
            catch.location_line = line
            catch.reproduce = reproduce_command(analysis.base_sha, analysis.head_sha, test.source)
            kept = _assess(catch, analysis, provider, config.use_judge)
            if isinstance(kept, Rejection):
                report.rejections.append(kept)
                continue
            report.catches.append(catch)
            if len(report.catches) >= config.max_catches:
                break
    return report


def _generate(analysis: DiffAnalysis, provider: Provider, report: Report) -> list[GeneratedTest]:
    tests: list[GeneratedTest] = []
    for label, generate in (
        ("intent-aware", generate_intent),
        ("dodgy-diff", generate_dodgy),
    ):
        try:
            tests.extend(generate(analysis, provider))
        except ProviderError as exc:
            report.warnings.append(f"{label} generator failed: {exc}")
    return tests


def _prepare(tests: list[GeneratedTest], report: Report) -> list[GeneratedTest]:
    seen: set[str] = set()
    unique: list[GeneratedTest] = []
    for test in tests:
        reason = validate_test_source(test.source)
        if reason:
            report.rejections.append(Rejection("static", reason, test.test_id, test.workflow))
            continue
        env = environment_source_reason(test.source)
        if env:
            report.rejections.append(Rejection("environment", env, test.test_id, test.workflow))
            continue
        key = ast.dump(ast.parse(test.source))
        if key in seen:
            report.rejections.append(
                Rejection(
                    "duplicate",
                    "identical test already considered",
                    test.test_id,
                    test.workflow,
                )
            )
            continue
        seen.add(key)
        unique.append(test)
    return unique


def _confirm(
    sandbox: Sandbox, test: GeneratedTest, repeats: int
) -> tuple[Catch | None, Rejection | None]:
    mutant_run: ProcResult | None = None
    first_base = sandbox.run_base(test.source)
    if not first_base.passed:
        return None, Rejection(
            "base",
            "test did not pass on the base commit",
            test.test_id,
            test.workflow,
        )
    if test.mutant is not None and test.workflow == "intent-aware":
        mutant_run = sandbox.run_mutant(test.mutant, test.source)
        if mutant_run.stderr.startswith("mutant "):
            return None, Rejection("mutant", mutant_run.stderr, test.test_id, test.workflow)
        if mutant_run.passed:
            return None, Rejection(
                "mutant",
                "test did not kill its mutant",
                test.test_id,
                test.workflow,
            )
    first_head = sandbox.run_head(test.source)
    if first_head.passed:
        return None, Rejection(
            "head",
            "test also passed on the head commit",
            test.test_id,
            test.workflow,
        )
    base_runs = [first_base]
    for _ in range(repeats - 1):
        result = sandbox.run_base(test.source)
        base_runs.append(result)
        if not result.passed:
            return None, Rejection(
                "base",
                "test was not stable on the base commit",
                test.test_id,
                test.workflow,
            )
    head_runs = [first_head]
    for _ in range(repeats - 1):
        result = sandbox.run_head(test.source)
        head_runs.append(result)
        if result.passed:
            return None, Rejection(
                "head",
                "test was not a stable failure on the head commit",
                test.test_id,
                test.workflow,
            )
    return (
        Catch(
            test=test,
            base_runs=tuple(base_runs),
            head_runs=tuple(head_runs),
            mutant_run=mutant_run,
            location_path="",
            location_line=1,
            reproduce="",
        ),
        None,
    )


def _assess(
    catch: Catch,
    analysis: DiffAnalysis,
    provider: Provider,
    use_judge: bool,
) -> Catch | Rejection:
    hits = (
        symbol_failure(catch, analysis),
        intended_change(catch, analysis),
        environment_failure(catch),
        import_failure(catch),
    )
    for hit in hits:
        if hit is not None:
            return hit
    if use_judge:
        judged = judge_catch(catch, analysis, provider)
        if judged is not None:
            return judged
    return catch


def _location(test: GeneratedTest, analysis: DiffAnalysis) -> tuple[str, int]:
    if test.mutant is not None:
        for item in analysis.files:
            if item.path != test.mutant.path:
                continue
            for sym in (*item.head_symbols, *item.base_symbols):
                if sym.qualname == test.mutant.symbol or sym.name == test.mutant.symbol:
                    line = sym.start_line
                    for head in item.head_symbols:
                        if head.name == sym.name:
                            line = head.start_line
                            break
                    return item.path, line
            return item.path, 1
    return analysis.primary_location()
