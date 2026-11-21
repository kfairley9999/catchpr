"""Embed catchbench transcripts into the static Worker page.

The page does not run tests. Outputs come from the published mock run when a
case produced them, and from one extra sandbox pass when the benchmark
discarded the test before saving stdout.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from catchbench.cases import CASES
from catchbench.harness import build_repo

from catchpr.runner import Sandbox

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
TEMPLATE = (ROOT / "template.html").read_text(encoding="utf-8")
MOCK = json.loads((REPO / "catchbench" / "results" / "mock.json").read_text(encoding="utf-8"))
BY_ID = {row["id"]: row for row in MOCK["cases"]}

# Which recorded test to show, and whether to execute it for the transcript.
DEMOS: tuple[tuple[str, str], ...] = (
    ("b02_discount_sign", "shown"),
    ("b08_rank_mutates", "shown"),
    ("b11_page_window", "run-intent"),
    ("r01_rename_locals", "run-intent"),
    ("r19_filtered_clock", "blocked"),
    ("r20_filtered_rename", "run-private"),
)


def _case(case_id: str):
    return next(item for item in CASES if item.id == case_id)


def _run(case_id: str, source: str) -> tuple[str, str, bool, bool]:
    case = _case(case_id)
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        base, head = build_repo(case, repo)
        with Sandbox(repo, base, head, timeout=20) as sandbox:
            base_run = sandbox.run_base(source)
            head_run = sandbox.run_head(source)
    return base_run.combined(), head_run.combined(), base_run.passed, head_run.passed


def _verdict(row: dict[str, object]) -> str:
    outcome = str(row["outcome"])
    if outcome == "catch":
        return (
            "catchpr would post one sticky comment. The test passed on the base "
            "commit and failed on the pull request, on every repetition."
        )
    if outcome == "filtered":
        return (
            "A candidate failure was not reported. An assessor rejected it, "
            "so the pull request stays without a catchpr comment."
        )
    return (
        "catchpr stays silent. The recorded test did not pass on the base and "
        "fail on the head, so there is nothing to prove."
    )


def build_cases() -> list[dict[str, object]]:
    payload: list[dict[str, object]] = []
    for case_id, mode in DEMOS:
        case = _case(case_id)
        row = BY_ID[case_id]
        test = case.intent_test
        base_output = str(row.get("base_output") or "")
        head_output = str(row.get("head_output") or "")
        base_passed = (
            bool(base_output) and "FAILED" not in base_output and "Error" not in base_output
        )
        head_passed = False
        if mode == "run-intent":
            base_output, head_output, base_passed, head_passed = _run(case_id, case.intent_test)
        elif mode == "run-private":
            test = case.dodgy[0][2]
            base_output, head_output, base_passed, head_passed = _run(case_id, test)
        elif mode == "blocked":
            test = case.dodgy[1][2]
            base_output = (
                "Not executed. The environment assessor rejected this test before the sandbox."
            )
            head_output = base_output
            base_passed = False
            head_passed = False
        elif mode == "shown":
            base_passed = True
            head_passed = False
        payload.append(
            {
                "id": case.id,
                "title": case.title,
                "kind": case.kind,
                "filename": case.filename,
                "outcome": row["outcome"],
                "diff": row["diff"],
                "test": test,
                "base_output": base_output,
                "head_output": head_output,
                "base_passed": base_passed,
                "head_passed": head_passed,
                "reasons": row["reasons"],
                "verdict": _verdict(row),
            }
        )
    return payload


def main() -> None:
    data = json.dumps(build_cases(), indent=2)
    html = TEMPLATE.replace("__CASES_JSON__", data)
    (ROOT / "playground.html").write_text(html, encoding="utf-8")
    worker = (
        "const HTML = "
        + json.dumps(html)
        + ";\nconst ROBOTS = "
        + json.dumps("User-agent: *\nAllow: /\n")
        + ";\nconst FAVICON = "
        + json.dumps(
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
            '<rect width="32" height="32" fill="#f3efe4"/>'
            '<rect x="1.5" y="1.5" width="29" height="29" fill="none" stroke="#9d2c14" stroke-width="1.5"/>'
            '<text x="16" y="23" text-anchor="middle" '
            'font-family="Georgia, Palatino, serif" font-size="18" fill="#1d1a16">C</text>'
            "</svg>"
        )
        + """;

const SECURITY = {
  "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'",
  "x-content-type-options": "nosniff",
  "referrer-policy": "no-referrer",
  "x-frame-options": "DENY",
};

function send(request, body, init) {
  const headers = { ...SECURITY, ...(init.headers || {}) };
  const payload = request.method === "HEAD" ? null : body;
  return new Response(payload, { ...init, headers });
}

export default {
  async fetch(request) {
    const method = request.method;
    if (method !== "GET" && method !== "HEAD") {
      return send(request, "Method not allowed", {
        status: 405,
        headers: { "content-type": "text/plain; charset=utf-8", allow: "GET, HEAD" },
      });
    }
    const path = new URL(request.url).pathname;
    if (path === "/robots.txt") {
      return send(request, ROBOTS, {
        headers: { "content-type": "text/plain; charset=utf-8", "cache-control": "public, max-age=86400" },
      });
    }
    if (path === "/favicon.svg" || path === "/favicon.ico") {
      return send(request, FAVICON, {
        headers: { "content-type": "image/svg+xml", "cache-control": "public, max-age=86400" },
      });
    }
    if (path === "/" || path === "") {
      return send(request, HTML, {
        headers: { "content-type": "text/html; charset=utf-8", "cache-control": "public, max-age=300" },
      });
    }
    return send(request, "Not found", {
      status: 404,
      headers: { "content-type": "text/plain; charset=utf-8" },
    });
  },
};
"""
    )
    out = ROOT / "src" / "worker.js"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(worker, encoding="utf-8")
    print(f"wrote {out} and playground.html ({len(data)} bytes of case data)")


if __name__ == "__main__":
    main()
