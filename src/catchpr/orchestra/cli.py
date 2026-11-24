"""``catchpr-orch``: run, inspect, verify, replay, approve, and resume pipelines."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from catchpr.orchestra.audit import BlobStore, utc_now, verify_run
from catchpr.orchestra.budget import BudgetLimits
from catchpr.orchestra.engine import Orchestrator, RunResult, load_resolutions
from catchpr.orchestra.errors import ConfigError, OrchestraError
from catchpr.orchestra.guard import (
    EffectGate,
    EffectLedger,
    EffectRequest,
    generate_keypair,
    issue_token,
    load_private_key,
    load_public_key,
    load_tokens,
)
from catchpr.orchestra.pipeline import Pipeline
from catchpr.orchestra.providers import Responder, deepinfra_key
from catchpr.orchestra.replay import replay_run
from catchpr.orchestra.tiers import build_tierset, default_tiers, load_tiers
from catchpr.orchestra.trace import html_trace, text_trace

EXIT_CODES = {
    "succeeded": 0,
    "needs_review": 10,
    "awaiting_approval": 11,
    "failed": 12,
    "budget_exceeded": 13,
}
EXIT_USAGE = 2
DEFAULT_KEY_DIR = Path(os.environ.get("CATCHPR_KEY_DIR", "~/.config/catchpr")).expanduser()
DEMOS = {
    "coi": "catchpr.orchestra.examples.coi",
    "leadflow": "catchpr.orchestra.examples.leadflow",
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    try:
        return int(args.func(args))
    except (OrchestraError, ValidationError, FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"catchpr-orch: {exc}", file=sys.stderr)
        return EXIT_USAGE


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="catchpr-orch", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    def run_options(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--runs-dir", type=Path, default=Path("runs"))
        sp.add_argument("--provider", choices=["auto", "deepinfra", "mock"], default="auto")
        sp.add_argument("--tiers", type=Path, help="tiers JSON file (default ladder if omitted)")
        sp.add_argument("--max-usd", default="0.50")
        sp.add_argument("--max-tokens", type=int, default=200_000)
        sp.add_argument("--max-seconds", type=float, default=900.0)
        sp.add_argument("--approvals", type=Path, help="JSON list of approval tokens")
        sp.add_argument("--public-key", type=Path, default=DEFAULT_KEY_DIR / "approver_ed25519.pub")
        sp.add_argument(
            "--ledger", type=Path, help="effects ledger (default RUNS/effects-ledger.jsonl)"
        )
        sp.add_argument("--parallel", type=int, default=1)
        sp.add_argument(
            "--no-payloads", action="store_true", help="store hashes only (no replay/resume)"
        )
        sp.add_argument("--html", action="store_true", help="also write trace.html")

    sp = sub.add_parser("run", help="run a pipeline on one input")
    sp.add_argument("--pipeline", required=True, help="module:factory returning a Pipeline")
    sp.add_argument("--input", type=Path, required=True)
    run_options(sp)
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("demo", help="run a bundled stub pipeline on synthetic data")
    sp.add_argument("name", choices=sorted(DEMOS))
    run_options(sp)
    sp.set_defaults(func=cmd_demo)

    sp = sub.add_parser("resume", help="re-run a stopped run, reusing finished steps")
    sp.add_argument("run_dir", type=Path)
    sp.add_argument("--pipeline", help="override the pipeline ref recorded in the run")
    run_options(sp)
    sp.set_defaults(func=cmd_resume)

    sp = sub.add_parser("trace", help="print a run's trace, or write HTML")
    sp.add_argument("run_dir", type=Path)
    sp.add_argument("--html", type=Path, help="write a self-contained HTML trace here")
    sp.set_defaults(func=cmd_trace)

    sp = sub.add_parser("verify", help="verify the audit hash chain and blobs")
    sp.add_argument("run_dir", type=Path)
    sp.set_defaults(func=cmd_verify)

    sp = sub.add_parser("replay", help="replay a run offline and compare outputs")
    sp.add_argument("run_dir", type=Path)
    sp.add_argument("--pipeline")
    sp.set_defaults(func=cmd_replay)

    sp = sub.add_parser("review", help="human review queue")
    rsub = sp.add_subparsers(dest="review_command", required=True)
    rl = rsub.add_parser("list")
    rl.add_argument("--runs-dir", type=Path, default=Path("runs"))
    rl.set_defaults(func=cmd_review_list)
    rr = rsub.add_parser("resolve", help="supply the output for a step that needs review")
    rr.add_argument("run_dir", type=Path)
    rr.add_argument("item")
    rr.add_argument("--output", type=Path, required=True)
    rr.add_argument("--reviewer", required=True)
    rr.add_argument("--pipeline")
    rr.set_defaults(func=cmd_review_resolve)

    sp = sub.add_parser("keys", help="approver key management")
    ksub = sp.add_subparsers(dest="keys_command", required=True)
    ki = ksub.add_parser("init")
    ki.add_argument("--dir", type=Path, default=DEFAULT_KEY_DIR)
    ki.set_defaults(func=cmd_keys_init)

    sp = sub.add_parser("approve", help="(human) sign an approval token for one queued effect")
    sp.add_argument("run_dir", type=Path)
    sp.add_argument("item")
    sp.add_argument("--approver", required=True)
    sp.add_argument("--key", type=Path, default=DEFAULT_KEY_DIR / "approver_ed25519")
    sp.add_argument("--tokens", type=Path, required=True, help="tokens file to append to")
    sp.add_argument("--ttl", type=int, default=3600)
    sp.add_argument("--yes", action="store_true", help="skip the interactive confirmation")
    sp.set_defaults(func=cmd_approve)

    sp = sub.add_parser("tiers", help="show the model ladder and which keys are present")
    sp.add_argument("--tiers", type=Path)
    sp.set_defaults(func=cmd_tiers)
    return p


# ---------------------------------------------------------------- helpers


def load_pipeline(ref: str, **kwargs: Any) -> Pipeline:
    module_name, _, attr = ref.partition(":")
    module = importlib.import_module(module_name)
    factory = getattr(module, attr or "build")
    pipeline = factory(**kwargs)
    if not isinstance(pipeline, Pipeline):
        raise ConfigError(f"{ref} did not return a Pipeline")
    return pipeline


def _limits(args: argparse.Namespace) -> BudgetLimits:
    try:
        usd = Decimal(str(args.max_usd))
    except InvalidOperation as exc:
        raise ConfigError(f"--max-usd {args.max_usd!r} is not a number") from exc
    return BudgetLimits(
        max_usd=usd,
        max_tokens=args.max_tokens,
        max_seconds=args.max_seconds if args.max_seconds > 0 else None,
    )


def _orchestrator(
    args: argparse.Namespace, pipeline: Pipeline, ref: str, responder: Responder | None
) -> Orchestrator:
    tiers = build_tierset(
        load_tiers(args.tiers) if args.tiers else None,
        mode=args.provider,
        env=os.environ,
        responder=responder,
    )
    public_key = load_public_key(args.public_key) if args.public_key.exists() else None
    gate = EffectGate(
        public_key=public_key,
        ledger=EffectLedger(args.ledger or args.runs_dir / "effects-ledger.jsonl"),
        executors=pipeline.effects,
        tokens=load_tokens(args.approvals) if args.approvals else [],
    )
    return Orchestrator(
        pipeline,
        tiers,
        runs_dir=args.runs_dir,
        limits=_limits(args),
        gate=gate,
        max_parallel=args.parallel,
        store_payloads=not args.no_payloads,
        pipeline_ref=ref,
    )


def _report(result: RunResult, html: bool) -> int:
    print(text_trace(result.run_dir))
    if html:
        out = result.run_dir / "trace.html"
        out.write_text(html_trace(result.run_dir), encoding="utf-8")
        print(f"\nHTML trace: {out}")
    for item in result.review_items:
        if item["kind"] == "approval":
            print(f"\napproval needed: {item['action']} -> {item['target']} (item {item['id']})")
            print(
                f"  catchpr-orch approve {result.run_dir} {item['id']} "
                "--approver <name> --tokens approvals.json"
            )
        else:
            print(f"\nreview needed: step {item['step']}: {item['reason']} (item {item['id']})")
    print(f"\nrun dir: {result.run_dir}")
    return EXIT_CODES[result.status]


def _demo_responder(ref: str) -> Responder | None:
    module = importlib.import_module(ref.partition(":")[0])
    responder = getattr(module, "demo_responder", None)
    return responder if callable(responder) else None


# --------------------------------------------------------------- commands


def cmd_run(args: argparse.Namespace) -> int:
    pipeline = load_pipeline(args.pipeline)
    data = json.loads(args.input.read_text(encoding="utf-8"))
    orch = _orchestrator(args, pipeline, args.pipeline, None)
    return _report(orch.run(data), args.html)


def cmd_demo(args: argparse.Namespace) -> int:
    module_name = DEMOS[args.name]
    ref = f"{module_name}:build"
    module = importlib.import_module(module_name)
    pipeline = load_pipeline(ref, outbox=args.runs_dir / "outbox")
    if args.provider == "auto":
        args.provider = "mock"  # demos are scripted; use --provider deepinfra for live calls
    responder = module.demo_responder if args.provider == "mock" else None
    orch = _orchestrator(args, pipeline, ref, responder)
    return _report(orch.run(module.SAMPLE_INPUT), args.html)


def cmd_resume(args: argparse.Namespace) -> int:
    summary = json.loads((args.run_dir / "summary.json").read_text(encoding="utf-8"))
    ref = args.pipeline or summary.get("pipeline_ref")
    if not ref:
        raise ConfigError("run has no recorded pipeline ref; pass --pipeline")
    kwargs = (
        {"outbox": args.runs_dir / "outbox"}
        if ref.startswith("catchpr.orchestra.examples.")
        else {}
    )
    pipeline = load_pipeline(ref, **kwargs)
    responder = None
    if args.provider in ("auto", "mock") and ref.startswith("catchpr.orchestra.examples."):
        args.provider = "mock"
        responder = _demo_responder(ref)
    orch = _orchestrator(args, pipeline, ref, responder)
    return _report(orch.resume(args.run_dir), args.html)


def cmd_trace(args: argparse.Namespace) -> int:
    if args.html:
        args.html.write_text(html_trace(args.run_dir), encoding="utf-8")
        print(f"wrote {args.html}")
    else:
        print(text_trace(args.run_dir))
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    report = verify_run(args.run_dir)
    print(
        f"{'OK' if report.ok else 'FAILED'}: {report.events} events, "
        f"{report.blobs_checked} blob references checked"
    )
    for error in report.errors:
        print(f"  - {error}")
    return 0 if report.ok else 1


def _pipeline_for(run_dir: Path, override: str | None) -> Pipeline:
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    ref = override or summary.get("pipeline_ref")
    if not ref:
        raise ConfigError("run has no recorded pipeline ref; pass --pipeline")
    return load_pipeline(ref)


def cmd_replay(args: argparse.Namespace) -> int:
    report = replay_run(args.run_dir, _pipeline_for(args.run_dir, args.pipeline))
    print(f"replay of {report.original_run}: {'MATCH' if report.match else 'MISMATCH'}")
    for sid, info in report.steps.items():
        print(
            f"  {sid:<12} {info['original']['status']:<18} -> "
            f"{info['replay']['status']:<18} {'ok' if info['match'] else 'DIFFERENT'}"
        )
    for diff in report.differences:
        print(f"  - {diff}")
    return 0 if report.match else 1


def _items(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "review.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _find_item(run_dir: Path, item_id: str) -> dict[str, Any]:
    for item in _items(run_dir):
        if item["id"] == item_id:
            return item
    raise ConfigError(f"no review item {item_id} in {run_dir}")


def cmd_review_list(args: argparse.Namespace) -> int:
    found = 0
    ledger = EffectLedger(args.runs_dir / "effects-ledger.jsonl")
    for run_dir in sorted(p for p in args.runs_dir.iterdir() if p.is_dir()):
        resolved = load_resolutions(run_dir)
        for item in _items(run_dir):
            found += 1
            if item["kind"] == "approval":
                state = "executed" if ledger.executed(item["request_id"]) else "open"
            else:
                state = "resolved" if item["step"] in resolved else "open"
            what = (
                f"{item['action']} -> {item['target']}"
                if item["kind"] == "approval"
                else item["reason"]
            )
            print(f"{item['id']}  [{item['kind']}/{state}]  {item['step']}: {what}")
    if not found:
        print("review queue is empty")
    return 0


def cmd_review_resolve(args: argparse.Namespace) -> int:
    item = _find_item(args.run_dir, args.item)
    if item["kind"] != "review":
        raise ConfigError("approval items are resolved with `catchpr-orch approve`")
    pipeline = _pipeline_for(args.run_dir, args.pipeline)
    model = pipeline.step(item["step"]).agent.output_model
    output = model.model_validate(json.loads(args.output.read_text(encoding="utf-8")))
    resolutions = load_resolutions(args.run_dir)
    resolutions[item["step"]] = {
        "output": output.model_dump(mode="json"),
        "reviewer": args.reviewer,
        "item": item["id"],
        "resolved_at": utc_now(),
    }
    path = args.run_dir / "resolutions.json"
    path.write_text(json.dumps(resolutions, indent=2, sort_keys=True), encoding="utf-8")
    os.chmod(path, 0o600)
    print(f"resolved {item['id']}; run `catchpr-orch resume {args.run_dir}` to continue")
    return 0


def cmd_keys_init(args: argparse.Namespace) -> int:
    priv, pub = generate_keypair(args.dir)
    print(f"private key (keep this with the human approver only): {priv}")
    print(f"public key (give this to the orchestrator): {pub}")
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    item = _find_item(args.run_dir, args.item)
    if item["kind"] != "approval":
        raise ConfigError(f"{args.item} is a review item, not an approval request")
    request = EffectRequest(item["action"], item["target"], item["payload"])
    if request.payload_sha256 != item["payload_sha256"]:
        raise ConfigError("queued payload does not match its recorded hash; refusing to sign")
    blobs = BlobStore(args.run_dir / "blobs", enabled=False)
    if blobs.has(item["payload_sha256"]) and blobs.get(item["payload_sha256"]) != item["payload"]:
        raise ConfigError("queued payload differs from the audited blob; refusing to sign")
    print(f"action : {request.action}\ntarget : {request.target}\npayload:")
    print(json.dumps(request.payload, indent=2, sort_keys=True))
    if not args.yes:
        answer = input("Approve exactly this effect? Type 'approve' to sign: ")
        if answer.strip() != "approve":
            print("not approved")
            return 1
    token = issue_token(
        load_private_key(args.key),
        request,
        approver=args.approver,
        ttl_seconds=args.ttl,
        note=item["id"],
    )
    tokens = load_tokens(args.tokens) if args.tokens.exists() else []
    tokens.append(token)
    args.tokens.write_text(json.dumps(tokens, indent=2), encoding="utf-8")
    os.chmod(args.tokens, 0o600)
    print(f"signed; token appended to {args.tokens} (valid {args.ttl}s, single use)")
    return 0


def cmd_tiers(args: argparse.Namespace) -> int:
    ladder = load_tiers(args.tiers) if args.tiers else default_tiers()
    has_key = deepinfra_key(os.environ) is not None
    print(f"DEEPINFRA_API_KEY: {'present' if has_key else 'missing (mock fallback)'}")
    print(f"ANTHROPIC_API_KEY: {'present' if os.environ.get('ANTHROPIC_API_KEY') else 'missing'}")
    for tier in ladder:
        price = (
            "unpriced"
            if tier.price is None
            else f"${tier.price.usd_per_m_input}/${tier.price.usd_per_m_output} per 1M in/out"
        )
        state = "enabled" if tier.enabled else f"DISABLED ({tier.disabled_reason})"
        print(f"  {tier.name:<9} {tier.provider:<10} {tier.model:<45} {price:<32} {state}")
    return 0
