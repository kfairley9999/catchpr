"""Features used by the real-team integrations, tested without the teams."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import BaseModel
from tests.orch_support import Num, double, make_orch

from catchpr.orchestra.agents import AgentContext, adapt
from catchpr.orchestra.audit import BlobStore
from catchpr.orchestra.engine import stable_digest
from catchpr.orchestra.errors import ConfigError
from catchpr.orchestra.pipeline import Pipeline, RetryPolicy, Step
from catchpr.orchestra.providers import IMAGE_TOKEN_ESTIMATE, LLMRequest, Message, MockProvider
from catchpr.orchestra.replay import replay_run


class Timed(BaseModel):
    value: int
    elapsed_s: float
    meta: dict[str, float] = {}


def test_extra_parameter_with_default_is_not_the_context() -> None:
    def team_style(inp: Num, provider: object | None = None) -> Num:
        assert provider is None
        return inp

    spec = adapt(team_style)
    assert not spec.takes_context

    def untyped_ctx(inp: Num, ctx) -> Num:  # type: ignore[no-untyped-def]
        return inp

    assert adapt(untyped_ctx).takes_context

    def required_extra(inp: Num, other: int) -> Num:
        return inp

    with pytest.raises(ConfigError, match="no default"):
        adapt(required_extra)

    def bad_hints(inp: Missing) -> Missing:  # type: ignore[name-defined]  # noqa: F821
        return inp

    assert adapt(bad_hints, input_model=Num, output_model=Num).input_model is Num


def test_bound_methods_adapt() -> None:
    class Agent:
        def run(self, inp: Num) -> Num:
            return Num(value=inp.value + 1)

    spec = adapt(Agent().run)
    assert spec.input_model is Num and not spec.takes_context


def test_blob_store_preserves_key_order(tmp_path: Path) -> None:
    store = BlobStore(tmp_path)
    sha = store.put({"b": 1, "a": {"z": 1, "y": 2}})
    assert list(store.get(sha)) == ["b", "a"]
    assert store.put({"a": {"y": 2, "z": 1}, "b": 1}) == sha  # same canonical digest
    (tmp_path / f"{sha}.json").write_text("{broken")
    with pytest.raises(ValueError):
        store.get(sha)


def test_volatile_fields_and_stable_replay(tmp_path: Path) -> None:
    counter = iter(range(100))

    def timed(x: Num) -> Timed:
        n = next(counter)
        return Timed(value=x.value, elapsed_s=n * 0.1, meta={"t": n})

    assert stable_digest(
        Timed(value=1, elapsed_s=1, meta={"t": 1}), ("elapsed_s", "meta.t")
    ) == stable_digest(Timed(value=1, elapsed_s=2, meta={"t": 9}), ("elapsed_s", "meta.t", "x.y"))
    p = Pipeline("v", Num, (Step("a", adapt(timed), volatile_fields=("elapsed_s", "meta.t")),))
    orch, _, _ = make_orch(tmp_path, p)
    result = orch.run({"value": 3})
    assert replay_run(result.run_dir, p, work_dir=tmp_path / "rp").match
    plain = Pipeline("v", Num, (Step("a", adapt(timed)),))
    orch2, _, _ = make_orch(tmp_path / "2", plain)
    result2 = orch2.run({"value": 3})
    assert not replay_run(result2.run_dir, plain, work_dir=tmp_path / "rp2").match


def test_confidence_of_routes_and_fails_closed(tmp_path: Path) -> None:
    def boom(out: BaseModel) -> float:
        raise RuntimeError("bad getter")

    p = Pipeline(
        "c",
        Num,
        (
            Step(
                "a",
                adapt(double),
                min_confidence=0.5,
                confidence_of=boom,
                escalate=False,
                retry=RetryPolicy(max_attempts=1),
            ),
        ),
    )
    orch, _, _ = make_orch(tmp_path, p)
    result = orch.run({"value": 1})
    assert result.steps["a"].status == "needs_review"
    assert result.steps["a"].confidence == 0.0
    ok = Pipeline(
        "c", Num, (Step("a", adapt(double), min_confidence=0.5, confidence_of=lambda o: 0.9),)
    )
    orch2, _, _ = make_orch(tmp_path / "2", ok)
    assert orch2.run({"value": 1}).steps["a"].confidence == 0.9


def test_allow_subprocess_only_opens_processes(tmp_path: Path) -> None:
    def shell(x: Num) -> Num:
        out = subprocess.run([sys.executable, "-c", "print(5)"], capture_output=True, text=True)
        return Num(value=int(out.stdout))

    p = Pipeline("s", Num, (Step("a", adapt(shell), allow_subprocess=True),))
    orch, _, _ = make_orch(tmp_path, p)
    assert orch.run({"value": 1}).output("a") == Num(value=5)


def test_pinned_model_and_images(tmp_path: Path) -> None:
    seen: list[LLMRequest] = []

    def respond(req: LLMRequest) -> str:
        seen.append(req)
        return json.dumps({"value": 1})

    def vision(x: Num, ctx: AgentContext) -> Num:
        res = ctx.llm.call(
            [("user", "read")],
            images_png=(b"\x89PNG fake",),
            model="Qwen/Qwen3-VL-235B-A22B-Instruct",
            max_tokens=10,
        )
        assert res.model == "Qwen/Qwen3-VL-235B-A22B-Instruct" and res.cost_usd > 0
        return Num(**json.loads(res.text))

    p = Pipeline("i", Num, (Step("a", adapt(vision)),))
    orch, _, _ = make_orch(tmp_path, p, MockProvider(responder=respond))
    result = orch.run({"value": 1})
    assert result.status == "succeeded"
    assert seen[0].as_dict()["images_sha256"][0].startswith("")
    assert (
        "images_sha256" in seen[0].as_dict()
        and b"PNG" not in json.dumps(seen[0].as_dict()).encode()
    )

    def unpriced(x: Num, ctx: AgentContext) -> Num:
        ctx.llm.call([("user", "x")], model="nobody/unknown-model")
        return x

    def empty(x: Num, ctx: AgentContext) -> Num:
        ctx.llm.call([])
        return x

    for fn in (unpriced, empty):
        p2 = Pipeline("i", Num, (Step("a", adapt(fn)),))
        orch2, prov2, _ = make_orch(tmp_path / fn.__name__, p2)
        assert orch2.run({"value": 1}).steps["a"].status == "failed"
        assert prov2.calls == []


def test_image_token_estimate() -> None:
    req = LLMRequest("m", (Message("user", "abc"),), 10, images_png=(b"x", b"y"))
    assert req.estimated_input_tokens() == 1 + 2 * IMAGE_TOKEN_ESTIMATE
