"""Score catchbench with a live OpenAI-compatible model.

Nothing this module prints is committed. Mock-mode numbers in
catchbench/results/mock.json are the only results shipped with the repository.
Cost is reported as an estimated token count, not a dollar figure.

Example:
  GROQ_API_KEY=... python -m catchbench.live --provider groq --limit 2
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from catchbench.cases import CASES
from catchbench.harness import cases_by_id, markdown_table, run_benchmark
from catchpr.providers.groq import DEFAULT_GROQ_MODEL, GroqProvider
from catchpr.providers.http import OpenAICompatibleProvider


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="catchbench.live")
    parser.add_argument("--provider", choices=("groq", "openai"), required=True)
    parser.add_argument("--model", default="")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--api-key-env", default="OPENAI_API_KEY")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--ids", default="")
    parser.add_argument("--output", type=Path, default=Path("catchbench/results/live.json"))
    args = parser.parse_args(argv)
    selected = CASES
    if args.ids:
        selected = cases_by_id([item.strip() for item in args.ids.split(",") if item.strip()])
    if args.limit:
        selected = selected[: args.limit]
    if args.provider == "groq":
        provider = GroqProvider(model=args.model or DEFAULT_GROQ_MODEL)
        name = "groq"
    else:
        provider = OpenAICompatibleProvider(
            name="openai",
            model=args.model or "gpt-4o-mini",
            base_url=args.base_url or "https://api.openai.com/v1",
            api_key=os.environ.get(args.api_key_env, ""),
        )
        name = "openai"
    summary = run_benchmark(
        cases=selected,
        repeats=args.repeats,
        workers=1,
        provider_name=name,
        live_provider=provider,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(markdown_table(summary))
    print(f"wrote {args.output}")
    print("Live results are gitignored. Do not commit them as if they were the published mock run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
