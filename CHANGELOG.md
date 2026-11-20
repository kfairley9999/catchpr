# Changelog

## 0.1.0 - 2026-11-20

First public version of catchpr. Python projects, pytest only.

- Diff analysis with `git` and `ast`, including the pull request title and body.
- Intent-aware and dodgy-diff generators behind a provider interface.
- `MockProvider` (default), `GroqProvider`, and a generic OpenAI-compatible provider,
  with a disk cache, a minimum interval, and retry on HTTP 429.
- Worktree sandbox, per-test subprocess, resource limits, and network blocking
  where Linux allows `unshare --net`. A catch requires the test to pass on the
  base 3/3 and fail on the head 3/3.
- Deterministic assessors for renamed or new symbols, behavior the pull request
  text declares, time / randomness / environment failures, and import errors.
  Optional LLM judge.
- Sticky markdown comment, SARIF 2.1, JSON, and CI exit codes.
- A split GitHub Action: the job that runs untrusted code cannot read secrets,
  and a `workflow_run` job posts the comment without executing the pull request.
- catchbench, forty fixture libraries, with published mock-mode results.
- A static Cloudflare Worker that replays cases in the browser.
