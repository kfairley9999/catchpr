"""Fail if a fresh mock run disagrees with the published result on outcomes.

Wall time and token counts are not compared. Those move between machines.
What must match is which cases catchpr would comment on.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        print("usage: python -m catchbench.compare published.json fresh.json", file=sys.stderr)
        return 2
    published = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    fresh = json.loads(Path(args[1]).read_text(encoding="utf-8"))
    problems: list[str] = []
    for key in ("bugs_caught", "false_alarms", "comments"):
        if published["catchpr"][key] != fresh["catchpr"][key]:
            problems.append(
                f"catchpr.{key}: published {published['catchpr'][key]} != fresh {fresh['catchpr'][key]}"
            )
    pub_cases = {row["id"]: row for row in published["cases"]}
    new_cases = {row["id"]: row for row in fresh["cases"]}
    if set(pub_cases) != set(new_cases):
        problems.append("case id sets differ")
    for case_id, row in pub_cases.items():
        other = new_cases.get(case_id)
        if other is None:
            continue
        if row["caught"] != other["caught"] or row["outcome"] != other["outcome"]:
            problems.append(
                f"{case_id}: published {row['outcome']}/{row['caught']} "
                f"!= fresh {other['outcome']}/{other['caught']}"
            )
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    print(
        f"outcomes match ({published['catchpr']['bugs_caught']} catches, "
        f"{published['catchpr']['false_alarms']} false alarms)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
