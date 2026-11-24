"""Readable views of a run: a terminal table and a self-contained HTML trace."""

from __future__ import annotations

import html
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from catchpr.orchestra.audit import read_events, verify_run

STATUS_COLORS = {
    "succeeded": "#1a7f37",
    "needs_review": "#9a6700",
    "awaiting_approval": "#8250df",
    "failed": "#cf222e",
    "budget_exceeded": "#cf222e",
    "skipped": "#6e7781",
    "not_run": "#6e7781",
}


def ordered_steps(summary: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Steps in pipeline order (summary.json stores them with sorted keys)."""
    order = [s["id"] for s in summary["pipeline"]["steps"]]
    steps = summary["steps"]
    return [(sid, steps[sid]) for sid in order if sid in steps]


def load_run(run_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    return summary, read_events(run_dir / "audit.jsonl")


def text_trace(run_dir: Path) -> str:
    summary, events = load_run(run_dir)
    report = verify_run(run_dir)
    lines = [
        f"run {summary['run_id']}  pipeline {summary['pipeline']['name']}  "
        f"status {summary['status'].upper()}",
        f"spent ${summary['budget']['spent_usd']} of ${summary['budget']['limit_max_usd']}  "
        f"tokens {summary['budget']['spent_tokens']}/{summary['budget']['limit_max_tokens']}  "
        f"elapsed {summary['budget']['elapsed_s']}s  "
        f"audit {'verified' if report.ok else 'FAILED VERIFY'} ({report.events} events)",
    ]
    lines += [f"note: {n}" for n in summary.get("notes", [])]
    lines.append("")
    header = f"{'step':<12} {'status':<18} {'tiers':<22} {'try':>3} {'tokens':>7} {'cost $':>10} {'conf':>5}"
    lines += [header, "-" * len(header)]
    for sid, info in ordered_steps(summary):
        conf = "" if info.get("confidence") is None else f"{info['confidence']:.2f}"
        lines.append(
            f"{sid:<12} {info['status']:<18} {'>'.join(info['tiers_tried']) or '-':<22} "
            f"{info['attempts']:>3} {info['tokens']:>7} {Decimal(info['cost_usd']):>10.6f} {conf:>5}"
        )
    lines.append("")
    lines.append("timeline:")
    for ev in events:
        lines.append("  " + _describe(ev))
    if summary.get("review_items"):
        lines.append("")
        lines.append("waiting on a human: " + ", ".join(summary["review_items"]))
    return "\n".join(lines)


def _describe(ev: dict[str, Any]) -> str:
    step = ev.get("step", "")
    return f"{ev['seq']:>3} {ev['ts'][11:23]} {ev['event']:<17} {step:<11} {_detail(ev)}".rstrip()


def _detail(ev: dict[str, Any]) -> str:
    kind = ev["event"]
    detail = {
        "run_start": lambda: f"pipeline={ev['pipeline']['name']} simulated={ev.get('simulated')}",
        "step_start": lambda: f"input={ev['input_sha256'][:12]}",
        "attempt_start": lambda: f"#{ev['attempt']} tier={ev['tier']} model={ev['model']}",
        "llm_call": lambda: (
            f"tier={ev['tier']} {ev['input_tokens']}+{ev['output_tokens']} tok "
            f"${ev['cost_usd']} {ev['latency_ms']}ms"
            + (" (simulated)" if ev.get("simulated") else "")
        ),
        "llm_error": lambda: f"{ev['error']}: {ev['message'][:80]}",
        "attempt_ok": lambda: (
            f"#{ev['attempt']} output={ev['output_sha256'][:12]} conf={ev.get('confidence')}"
        ),
        "attempt_error": lambda: (
            f"#{ev['attempt']} {ev['error']}: {ev['message'][:80]}"
            + (f" retry in {ev.get('backoff_s')}s" if ev.get("retryable") else "")
        ),
        "low_confidence": lambda: f"{ev['confidence']} < {ev['threshold']} on {ev['tier']}",
        "escalate": lambda: f"{ev['from_tier']} -> {ev['to_tier']}",
        "tier_skipped": lambda: f"{ev['tier']} ({ev.get('reason')})",
        "approval_required": lambda: f"{ev['action']} -> {ev['target']} item={ev['item']}",
        "effect_executed": lambda: f"{ev['action']} -> {ev['target']} by {ev['approver']}",
        "review_queued": lambda: f"item={ev['item']} {ev['reason'][:80]}",
        "step_end": lambda: f"{ev.get('status')}",
        "step_skipped": lambda: f"blocked by {ev.get('blocked_by')}",
        "step_override": lambda: f"source={ev['source']}",
        "run_end": lambda: f"{ev['status']} spent=${ev['budget']['spent_usd']}",
    }.get(kind)
    return detail() if detail else ""


def html_trace(run_dir: Path) -> str:
    summary, events = load_run(run_dir)
    report = verify_run(run_dir)
    e = html.escape
    rows = []
    for sid, info in ordered_steps(summary):
        color = STATUS_COLORS.get(info["status"], "#6e7781")
        conf = "" if info.get("confidence") is None else f"{info['confidence']:.2f}"
        rows.append(
            f"<tr><td>{e(sid)}</td><td><span class=b style='background:{color}'>"
            f"{e(info['status'])}</span></td><td>{e(' → '.join(info['tiers_tried']))}</td>"
            f"<td class=n>{info['attempts']}</td><td class=n>{info['tokens']}</td>"
            f"<td class=n>${Decimal(info['cost_usd']):.6f}</td><td class=n>{e(conf)}</td>"
            f"<td>{e(info.get('error') or '')}</td></tr>"
        )
    timeline = "".join(
        f"<tr class='ev {e(ev['event'])}'><td class=n>{ev['seq']}</td><td>{e(ev['ts'])}</td>"
        f"<td>{e(ev['event'])}</td><td>{e(ev.get('step', ''))}</td>"
        f"<td>{e(_detail(ev))}</td></tr>"
        for ev in events
    )
    notes = "".join(f"<li>{e(n)}</li>" for n in summary.get("notes", []))
    verified = (
        "<span class=b style='background:#1a7f37'>hash chain verified</span>"
        if report.ok
        else "<span class=b style='background:#cf222e'>verification failed</span> "
        + e("; ".join(report.errors[:3]))
    )
    status_color = STATUS_COLORS.get(summary["status"], "#6e7781")
    b = summary["budget"]
    return f"""<!doctype html>
<html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>catchpr trace {e(summary["run_id"])}</title>
<style>
body{{font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif;margin:32px auto;max-width:1100px;padding:0 16px;color:#1f2328}}
h1{{font-size:20px;margin:0 0 4px}} .muted{{color:#656d76}} table{{border-collapse:collapse;width:100%;margin:12px 0 28px}}
th,td{{text-align:left;padding:6px 8px;border-bottom:1px solid #d8dee4;vertical-align:top}} th{{font-weight:600;background:#f6f8fa}}
.n{{text-align:right;font-variant-numeric:tabular-nums}} .b{{color:#fff;border-radius:10px;padding:1px 8px;font-size:12px}}
.cards{{display:flex;gap:12px;flex-wrap:wrap;margin:16px 0}} .card{{border:1px solid #d8dee4;border-radius:8px;padding:10px 14px;min-width:150px}}
.card b{{display:block;font-size:18px}} tr.ev td{{font-size:12.5px;font-family:ui-monospace,Menlo,monospace}}
tr.escalate,tr.low_confidence{{background:#fff8c5}} tr.attempt_error,tr.llm_error{{background:#ffebe9}} tr.approval_required,tr.review_queued{{background:#fbefff}}
</style></head><body>
<h1>{e(summary["pipeline"]["name"])} <span class=b style='background:{status_color}'>{e(summary["status"])}</span></h1>
<div class=muted>run {e(summary["run_id"])} · {verified}</div>
<div class=cards>
<div class=card>spent<b>${e(b["spent_usd"])}</b><span class=muted>of ${e(str(b["limit_max_usd"]))}</span></div>
<div class=card>tokens<b>{b["spent_tokens"]}</b><span class=muted>of {b["limit_max_tokens"]}</span></div>
<div class=card>elapsed<b>{b["elapsed_s"]}s</b></div>
<div class=card>events<b>{report.events}</b></div>
</div>
<ul class=muted>{notes}</ul>
<h2>Steps</h2>
<table><tr><th>step</th><th>status</th><th>tiers</th><th class=n>tries</th><th class=n>tokens</th><th class=n>cost</th><th class=n>conf</th><th>error</th></tr>
{"".join(rows)}</table>
<h2>Audit timeline</h2>
<table><tr><th class=n>#</th><th>time (UTC)</th><th>event</th><th>step</th><th>detail</th></tr>{timeline}</table>
</body></html>
"""
