from __future__ import annotations

import json
import re
from pathlib import Path

from catchbench.cases import CASES, assert_catalog

ROOT = Path(__file__).resolve().parents[1]
SHA = re.compile(r"uses: (?P<action>[^\s@]+)@(?P<sha>[0-9a-f]{40})(?: #|$)")


def test_catalog_has_twenty_bugs_and_twenty_refactors() -> None:
    assert_catalog()
    assert len(CASES) == 40


def test_workflows_are_pinned_and_do_not_use_pull_request_target() -> None:
    workflows = list((ROOT / ".github" / "workflows").glob("*.yml"))
    assert workflows
    for path in workflows:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"(?m)^\s*pull_request_target\s*:", text)
        for match in SHA.finditer(text):
            assert len(match.group("sha")) == 40
        assert "@v" not in text.split("uses:")[1] if "uses:" in text else True
        # No floating tags. Every uses: line is a SHA pin.
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("uses:"):
                assert re.search(r"@[0-9a-f]{40}\b", stripped), stripped
    untrusted = (ROOT / ".github" / "workflows" / "catchpr.yml").read_text(encoding="utf-8")
    assert "contents: read" in untrusted
    assert "${{ secrets" not in untrusted
    assert "pull_request:" in untrusted
    comment = (ROOT / ".github" / "workflows" / "catchpr-comment.yml").read_text(encoding="utf-8")
    assert "workflow_run:" in comment
    assert "pull-requests: write" in comment
    assert "pytest" not in comment
    action = (ROOT / "action.yml").read_text(encoding="utf-8")
    assert "${{ secrets" not in action
    assert "pull_request_target" not in action
    wrangler = (ROOT / "demo" / "wrangler.toml").read_text(encoding="utf-8")
    assert "[[kv_namespaces]]" not in wrangler
    assert "r2_buckets" not in wrangler
    assert "d1_databases" not in wrangler
    worker = (ROOT / "demo" / "src" / "worker.js").read_text(encoding="utf-8")
    assert "export default" in worker
    # The page may link to GitHub and use the SVG namespace. The Worker itself
    # must not issue HTTP requests.
    assert ".fetch(" not in worker
    assert "XMLHttpRequest" not in worker
    assert "WebSocket" not in worker


def test_published_mock_benchmark_matches_the_readme() -> None:
    data = json.loads((ROOT / "catchbench" / "results" / "mock.json").read_text(encoding="utf-8"))
    assert data["mode"] == "mock"
    assert data["catchpr"]["bugs_caught"] == 16
    assert data["catchpr"]["false_alarms"] == 0
    assert data["catchpr"]["comments"] == 16
    assert data["catchpr"]["api_cost_usd"] == 0.0
    assert data["catchpr"]["remote_calls"] == 0
    assert data["baseline_readonly_review"]["bugs_flagged"] == 14
    assert data["baseline_readonly_review"]["false_alarms"] == 6
    assert "not a measurement of a live model" in data["baseline_readonly_review"]["note"]
    assert len(data["cases"]) == 40
