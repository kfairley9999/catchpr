"""Command line interface."""

from __future__ import annotations

import argparse
import base64
import os
import sys
from pathlib import Path

from catchpr import __version__
from catchpr.baseline import baseline_review
from catchpr.diff import analyze_repo
from catchpr.errors import CatchprError, UsageError
from catchpr.generate import validate_test_source
from catchpr.github_api import fetch_artifact, post_sticky
from catchpr.gitutil import ensure_repo, rev_parse
from catchpr.pipeline import RunConfig, run_pipeline
from catchpr.providers.base import Provider
from catchpr.providers.cache import CachingProvider
from catchpr.providers.groq import DEFAULT_GROQ_MODEL, GroqProvider
from catchpr.providers.http import OpenAICompatibleProvider
from catchpr.providers.mock import MockProvider, load_recordings
from catchpr.report import (
    catch_count,
    comment_from_payload,
    comment_markdown,
    load_payload,
    report_json,
    sarif_json,
)
from catchpr.runner import Sandbox

EXIT_OK = 0
EXIT_CATCH = 1
EXIT_USAGE = 2
EXIT_FAILURE = 3


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        return code if isinstance(code, int) else EXIT_USAGE
    try:
        return int(args.func(args))
    except UsageError as exc:
        print(f"catchpr: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except CatchprError as exc:
        print(f"catchpr: {exc}", file=sys.stderr)
        return EXIT_FAILURE


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="catchpr",
        description=(
            "Generate catching tests for a pull request and comment only when "
            "one passes on the base commit and fails on the head."
        ),
    )
    parser.add_argument("--version", action="version", version=f"catchpr {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="analyze a diff and validate generated tests")
    _add_repo_args(run)
    _add_provider_args(run)
    run.add_argument("--title", default="", help="pull request title")
    run.add_argument("--body", default="", help="pull request body")
    run.add_argument("--body-file", type=Path, help="read the pull request body from a file")
    run.add_argument("--repeats", type=int, default=3, help="passes required on each side")
    run.add_argument("--timeout", type=float, default=20.0, help="seconds per test process")
    run.add_argument("--judge", action="store_true", help="ask the provider to assess catches")
    run.add_argument("--max-catches", type=int, default=5)
    run.add_argument("--json", type=Path, dest="json_path", help="write the JSON report")
    run.add_argument("--sarif", type=Path, help="write a SARIF 2.1 report")
    run.add_argument("--comment", type=Path, help="write the sticky comment markdown")
    run.add_argument(
        "--report-only",
        action="store_true",
        help="exit 0 even when catches are found",
    )
    run.add_argument(
        "--allow-network",
        action="store_true",
        help="do not isolate test processes from the network",
    )
    run.set_defaults(func=_cmd_run)

    repro = sub.add_parser("repro", help="re-run one test on base and head")
    _add_repo_args(repro)
    source = repro.add_mutually_exclusive_group(required=True)
    source.add_argument("--test", type=Path, help="pytest file to run")
    source.add_argument("--test-b64", help="base64-encoded pytest module")
    repro.add_argument("--timeout", type=float, default=20.0)
    repro.set_defaults(func=_cmd_repro)

    comment = sub.add_parser("comment", help="post or delete the sticky pull request comment")
    comment.add_argument("--results", type=Path, required=True)
    comment.add_argument("--repository", required=True, help="owner/name")
    comment.add_argument("--pr", type=int, required=True)
    comment.add_argument("--api", default="https://api.github.com")
    comment.set_defaults(func=_cmd_comment)

    fetch = sub.add_parser("fetch-artifact", help="download a workflow artifact and extract it")
    fetch.add_argument("--repository", required=True)
    fetch.add_argument("--run-id", type=int, required=True)
    fetch.add_argument("--name", required=True)
    fetch.add_argument("--dest", type=Path, required=True)
    fetch.add_argument("--api", default="https://api.github.com")
    fetch.set_defaults(func=_cmd_fetch)

    baseline = sub.add_parser("baseline", help="read-only review, no test execution")
    _add_repo_args(baseline)
    _add_provider_args(baseline)
    baseline.add_argument("--title", default="")
    baseline.add_argument("--body", default="")
    baseline.add_argument("--body-file", type=Path)
    baseline.set_defaults(func=_cmd_baseline)
    return parser


def _add_repo_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--base", required=True, help="base revision")
    parser.add_argument("--head", required=True, help="head revision")


def _add_provider_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--provider",
        choices=("mock", "groq", "openai"),
        default="mock",
    )
    parser.add_argument("--model", default="")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--api-key-env", default="OPENAI_API_KEY")
    parser.add_argument("--mock-recordings", type=Path)
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--min-interval", type=float, default=-1.0)


def _cmd_run(args: argparse.Namespace) -> int:
    if args.repeats < 1:
        raise UsageError("--repeats must be >= 1")
    ensure_repo(Path(args.repo))
    provider = _provider(args)
    body = _body(args)
    report = run_pipeline(
        RunConfig(
            repo=args.repo,
            base=args.base,
            head=args.head,
            title=args.title,
            body=body,
            repeats=args.repeats,
            timeout=args.timeout,
            use_judge=bool(args.judge),
            max_catches=args.max_catches,
            block_network=not bool(args.allow_network),
        ),
        provider,
    )
    rendered_json = report_json(report)
    rendered_sarif = sarif_json(report)
    rendered_comment = comment_markdown(report)
    if args.json_path:
        args.json_path.write_text(rendered_json, encoding="utf-8")
    if args.sarif:
        args.sarif.write_text(rendered_sarif, encoding="utf-8")
    if args.comment:
        args.comment.write_text(rendered_comment, encoding="utf-8")
    if report.catches:
        print(f"catchpr: {len(report.catches)} validated catch(es) on {report.head_sha[:12]}")
        return EXIT_OK if args.report_only else EXIT_CATCH
    print(f"catchpr: no validated catches on {report.head_sha[:12]}")
    for warning in report.warnings:
        print(f"catchpr: warning: {warning}", file=sys.stderr)
    if report.warnings and not report.catches and _provider_failed(report.warnings):
        return EXIT_FAILURE
    return EXIT_OK


def _provider_failed(warnings: list[str]) -> bool:
    return any("generator failed" in warning for warning in warnings) and len(warnings) >= 2


def _cmd_repro(args: argparse.Namespace) -> int:
    source = _test_source(args)
    reason = validate_test_source(source)
    if reason:
        raise UsageError(reason)
    repo = Path(args.repo)
    ensure_repo(repo)
    base = rev_parse(repo, args.base)
    head = rev_parse(repo, args.head)
    with Sandbox(repo, base, head, timeout=args.timeout) as sandbox:
        base_run = sandbox.run_base(source)
        head_run = sandbox.run_head(source)
    print(f"base {base[:12]} {'PASS' if base_run.passed else 'FAIL'}")
    print(base_run.combined())
    print(f"head {head[:12]} {'PASS' if head_run.passed else 'FAIL'}")
    print(head_run.combined())
    if base_run.passed and not head_run.passed:
        print("catchpr: reproduced")
        return EXIT_OK
    print("catchpr: did not reproduce")
    return EXIT_CATCH


def _cmd_comment(args: argparse.Namespace) -> int:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    if not token:
        raise UsageError("GITHUB_TOKEN is not set")
    payload = load_payload(args.results.read_text(encoding="utf-8"))
    body = comment_from_payload(payload)
    action = post_sticky(
        repository=args.repository,
        pr=args.pr,
        body=body,
        token=token,
        api=args.api,
    )
    print(f"catchpr: comment {action} ({catch_count(payload)} catch(es))")
    return EXIT_OK


def _cmd_fetch(args: argparse.Namespace) -> int:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    if not token:
        raise UsageError("GITHUB_TOKEN is not set")
    dest = fetch_artifact(
        repository=args.repository,
        run_id=args.run_id,
        name=args.name,
        dest=args.dest,
        token=token,
        api=args.api,
    )
    print(f"catchpr: extracted artifact to {dest}")
    return EXIT_OK


def _cmd_baseline(args: argparse.Namespace) -> int:
    ensure_repo(Path(args.repo))
    provider = _provider(args)
    analysis = analyze_repo(
        args.repo,
        args.base,
        args.head,
        title=args.title,
        body=_body(args),
    )
    verdict, summary = baseline_review(analysis, provider)
    print(verdict)
    if summary:
        print(summary)
    return EXIT_OK


def _body(args: argparse.Namespace) -> str:
    if getattr(args, "body_file", None):
        return Path(args.body_file).read_text(encoding="utf-8")
    return str(getattr(args, "body", "") or "")


def _test_source(args: argparse.Namespace) -> str:
    if args.test_b64:
        try:
            return base64.b64decode(args.test_b64, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise UsageError(f"--test-b64 is not valid: {exc}") from exc
    return Path(args.test).read_text(encoding="utf-8")


def _provider(args: argparse.Namespace) -> Provider:
    interval = args.min_interval
    if args.provider == "mock":
        recordings = load_recordings(args.mock_recordings) if args.mock_recordings else []
        inner: Provider = MockProvider(recordings, model=args.model or "recorded")
        if interval < 0:
            interval = 0.0
    elif args.provider == "groq":
        inner = GroqProvider(
            model=args.model or DEFAULT_GROQ_MODEL,
            min_interval=0.25 if interval < 0 else interval,
        )
    elif args.provider == "openai":
        key = os.environ.get(args.api_key_env, "")
        inner = OpenAICompatibleProvider(
            name="openai",
            model=args.model or "gpt-4o-mini",
            base_url=args.base_url or "https://api.openai.com/v1",
            api_key=key,
            min_interval=0.0 if interval < 0 else interval,
        )
    else:
        raise UsageError(f"unknown provider {args.provider}")
    if args.no_cache:
        return inner
    cache_dir = args.cache_dir or (Path.home() / ".cache" / "catchpr")
    return CachingProvider(inner, cache_dir)
