"""Markdown comment, SARIF 2.1, and JSON for one catchpr run."""

from __future__ import annotations

import json
import re

from catchpr import __version__
from catchpr.jsonutil import as_dict, as_list, as_str, loads
from catchpr.models import Catch, Rejection, Report

_MAX_COMMENT_CATCHES = 5
_MAX_SOURCE = 8_000
_MAX_OUTPUT = 4_000
_MAX_BODY = 60_000


def reproduce_command(base_sha: str, head_sha: str, source: str) -> str:
    import base64

    encoded = base64.b64encode(source.encode()).decode("ascii")
    return f"catchpr repro --repo . --base {base_sha} --head {head_sha} --test-b64 {encoded}"


def report_payload(report: Report) -> dict[str, object]:
    return {
        "version": __version__,
        "base": report.base_sha,
        "head": report.head_sha,
        "title": report.title,
        "silent": report.silent,
        "warnings": list(report.warnings),
        "catches": [_catch_payload(catch) for catch in report.catches],
        "rejections": [
            {
                "filter": item.filter_name,
                "reason": item.reason,
                "test_id": item.test_id,
                "workflow": item.workflow,
            }
            for item in report.rejections
        ],
    }


def report_json(report: Report) -> str:
    return json.dumps(report_payload(report), indent=2) + "\n"


def to_sarif(report: Report) -> dict[str, object]:
    results: list[object] = []
    for catch in report.catches:
        message = (
            f"{catch.test.workflow} test {catch.test.test_id} passes on "
            f"{report.base_sha[:12]} and fails on {report.head_sha[:12]}."
        )
        results.append(
            {
                "ruleId": "catchpr/behavior-change",
                "level": "error",
                "message": {"text": message},
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {"uri": catch.location_path},
                            "region": {"startLine": max(1, catch.location_line)},
                        }
                    }
                ],
                "properties": {
                    "workflow": catch.test.workflow,
                    "testId": catch.test.test_id,
                    "reproduce": catch.reproduce,
                },
            }
        )
    return {
        "version": "2.1.0",
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "catchpr",
                        "version": __version__,
                        "informationUri": "https://arxiv.org/abs/2601.22832",
                        "rules": [
                            {
                                "id": "catchpr/behavior-change",
                                "shortDescription": {
                                    "text": (
                                        "A generated test passes on the base "
                                        "commit and fails on the head commit."
                                    )
                                },
                            }
                        ],
                    }
                },
                "results": results,
            }
        ],
    }


def sarif_json(report: Report) -> str:
    return json.dumps(to_sarif(report), indent=2) + "\n"


def comment_markdown(report: Report) -> str:
    if report.silent:
        return ""
    return _render_comment(
        base=report.base_sha,
        head=report.head_sha,
        catches=[_catch_payload(catch) for catch in report.catches],
    )


def comment_from_payload(payload: object) -> str:
    """Rebuild the sticky comment from JSON. Untrusted strings are fenced."""
    data = as_dict(payload)
    if data is None:
        raise ValueError("results payload must be an object")
    catches_raw = as_list(data.get("catches")) or []
    catches: list[dict[str, object]] = []
    for item in catches_raw:
        row = as_dict(item)
        if row is not None:
            catches.append(row)
    if not catches:
        return ""
    base = as_str(data.get("base")) or "BASE"
    head = as_str(data.get("head")) or "HEAD"
    return _render_comment(base=base, head=head, catches=catches)


def load_payload(text: str) -> dict[str, object]:
    data = as_dict(loads(text))
    if data is None:
        raise ValueError("results file must be a JSON object")
    return data


def _catch_payload(catch: Catch) -> dict[str, object]:
    base_out = catch.base_runs[-1].combined() if catch.base_runs else ""
    head_out = catch.head_runs[-1].combined() if catch.head_runs else ""
    return {
        "workflow": catch.test.workflow,
        "test_id": catch.test.test_id,
        "rationale": catch.test.rationale,
        "source": catch.test.source,
        "location_path": catch.location_path,
        "location_line": catch.location_line,
        "reproduce": catch.reproduce,
        "base_runs": len(catch.base_runs),
        "head_runs": len(catch.head_runs),
        "base_output": base_out,
        "head_output": head_out,
    }


def _render_comment(*, base: str, head: str, catches: list[dict[str, object]]) -> str:
    from catchpr import STICKY_MARKER

    shown = catches[:_MAX_COMMENT_CATCHES]
    hidden = len(catches) - len(shown)
    lines = [
        STICKY_MARKER,
        "## catchpr found a behavior change",
        "",
        "A generated test passes on the base commit and fails on this pull request,",
        f"on every repetition ({_runs_label(shown)}). That is evidence of a behavior",
        "change. It is not, by itself, proof of a bug. If the new behavior is what",
        "you meant, dismiss the comment. If it is not, the command below reproduces it.",
        "",
        f"Base `{base}` · head `{head}`",
        "",
    ]
    for index, catch in enumerate(shown, start=1):
        workflow = _clip_inline(as_str(catch.get("workflow")) or "workflow")
        test_id = _clip_inline(as_str(catch.get("test_id")) or "test")
        path = _clip_inline(as_str(catch.get("location_path")) or "")
        rationale = as_str(catch.get("rationale")) or ""
        source = _cap(as_str(catch.get("source")) or "", _MAX_SOURCE)
        base_out = _cap(as_str(catch.get("base_output")) or "passed", _MAX_OUTPUT)
        head_out = _cap(as_str(catch.get("head_output")) or "", _MAX_OUTPUT)
        reproduce = as_str(catch.get("reproduce")) or ""
        lines.append(f"### {index}. {workflow} `{test_id}`")
        if path:
            lines.append(f"Location: `{path}`")
        if rationale:
            lines.append("")
            lines.append(_neutralize(rationale.strip())[:500])
        lines.append("")
        lines.append("Test:")
        lines.append("")
        lines.append(_fence(_neutralize(source), "python"))
        lines.append("")
        lines.append("Base output:")
        lines.append("")
        lines.append(_fence(_neutralize(base_out), "text"))
        lines.append("")
        lines.append("Head output:")
        lines.append("")
        lines.append(_fence(_neutralize(head_out), "text"))
        lines.append("")
        lines.append("Reproduce:")
        lines.append("")
        lines.append(_fence(reproduce, "bash"))
        lines.append("")
    if hidden > 0:
        lines.append(f"{hidden} more catch(es) are in the JSON artifact.")
        lines.append("")
    lines.append(
        "catchpr comments only when a test passes on the base and fails on the head. "
        "If this thread is stale, the next run deletes it."
    )
    body = "\n".join(lines).strip() + "\n"
    if len(body) > _MAX_BODY:
        body = body[: _MAX_BODY - 20] + "\n... truncated ...\n"
    return body


def _runs_label(catches: list[dict[str, object]]) -> str:
    if not catches:
        return "repeated"
    first = catches[0]
    base_runs = first.get("base_runs")
    head_runs = first.get("head_runs")
    if isinstance(base_runs, int) and isinstance(head_runs, int) and base_runs and head_runs:
        return f"{base_runs}/{base_runs} base, {head_runs}/{head_runs} head"
    return "repeated"


def _cap(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... truncated ..."


def _clip_inline(text: str) -> str:
    cleaned = text.replace("\n", " ").replace("`", "'")
    return cleaned[:120]


def _neutralize(text: str) -> str:
    """Break @mentions so a test failure cannot ping a GitHub user or team."""
    return text.replace("@", "@\u200b")


def _fence(text: str, lang: str) -> str:
    longest = 2
    for match in re.finditer(r"`+", text):
        longest = max(longest, len(match.group(0)))
    ticks = "`" * (longest + 1)
    return f"{ticks}{lang}\n{text.rstrip()}\n{ticks}"


def rejections_of(payload: dict[str, object]) -> list[Rejection]:
    rows = as_list(payload.get("rejections")) or []
    found: list[Rejection] = []
    for item in rows:
        data = as_dict(item)
        if data is None:
            continue
        found.append(
            Rejection(
                filter_name=as_str(data.get("filter")) or "",
                reason=as_str(data.get("reason")) or "",
                test_id=as_str(data.get("test_id")) or "",
                workflow=as_str(data.get("workflow")) or "",
            )
        )
    return found


def catch_count(payload: dict[str, object]) -> int:
    rows = as_list(payload.get("catches"))
    return len(rows) if rows else 0
