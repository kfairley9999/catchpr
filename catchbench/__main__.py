"""python -m catchbench writes catchbench/results/mock.json."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from catchbench.harness import cases_by_id, markdown_table, run_benchmark


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="catchbench")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--workers", type=int, default=max(1, min(4, os.cpu_count() or 1)))
    parser.add_argument("--limit", type=int, default=0, help="run only the first N cases")
    parser.add_argument("--ids", default="", help="comma-separated case ids")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("catchbench/results/mock.json"),
    )
    args = parser.parse_args(argv)
    from catchbench.cases import CASES

    selected = CASES
    if args.ids:
        selected = cases_by_id([item.strip() for item in args.ids.split(",") if item.strip()])
    if args.limit:
        selected = selected[: args.limit]
    summary = run_benchmark(cases=selected, repeats=args.repeats, workers=args.workers)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(markdown_table(summary))
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
