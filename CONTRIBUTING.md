# Contributing

catchpr is a v0.1 tool for Python projects tested with pytest. Changes should
keep the silence property: the tool comments only when a generated test passes
on the base commit and fails on the head commit, repeatedly, and survives the
assessors.

## Setup

```bash
python -m pip install -e ".[dev]"
python -m ruff check src tests catchbench
python -m ruff format --check src tests catchbench
python -m mypy --strict src
python -m pytest --cov=catchpr --cov-fail-under=90
```

Python 3.10 through 3.13 are the supported range. The sandbox uses `resource`
and, on Linux, `unshare`. Both are available in CI.

## The benchmark

`python -m catchbench` replays the mock benchmark and writes
`catchbench/results/mock.json`. That file is the published result. A change
that moves a case from silent to catch, or the other way, has to update the
file and the table in the README, and the README has to stay explicit that
the numbers are from recorded responses.

`python -m catchbench.live` calls a real provider. Its output is gitignored.
Do not commit live numbers you did not produce by running that command, and
do not invent them.

`python -m catchbench.compare published.json fresh.json` checks that the
outcomes match. It ignores wall time.

## Providers

`MockProvider` is the default. Recordings are JSON objects with `contains`
(substrings that must all appear in the prompt) and `response`. An unknown
prompt returns an empty payload so the run stays silent. HTTP providers retry
on 429 and cache under `--cache-dir`.

## Pull requests

The `catchpr` workflow runs the tool with `contents: read` and no secrets.
The `catchpr-comment` workflow posts the comment and must not start running
pytest. Do not introduce `pull_request_target`. Pin any new action to a commit
SHA.

Do not add a `Co-authored-by` trailer. Do not commit API keys, personal email
addresses, or phone numbers.

## Not in this repository

This package is not published to PyPI.
