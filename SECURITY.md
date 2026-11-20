# Security

catchpr runs tests against a pull request. That code is untrusted. The threat
model below is the one the GitHub Action is built around. v0.1 is a local tool
and a CI pattern, not a sandbox that can hold a hostile program.

## What we are protecting

- Secrets on the runner. A pull request must not be able to read API keys,
  the default `GITHUB_TOKEN`'s write scope, or anything else the maintainer
  did not mean to hand to a stranger.
- The comment step. Posting to a pull request is a privileged action. It must
  not be done by a process that is executing the pull request.
- The maintainer's other repositories. A comment body is data. It must not be
  able to ping arbitrary users or smuggle a second workflow into running.

## What a catch is

A generated test is executed, in a subprocess, on a detached worktree of the
base commit and on a detached worktree of the head commit. The tool comments
only when that test passes on the base every time and fails on the head every
time, and none of the assessors reject it. Silence is the default.

## The two jobs

`pull_request` workflow (`.github/workflows/catchpr.yml`):

- Triggers on `pull_request`, never on `pull_request_target`.
- `permissions: contents: read`.
- No `secrets` context. The default provider is `mock`, which needs no key.
- Checks out the pull request, installs catchpr, runs it, uploads a JSON artifact.
- This job executes untrusted code: the project under test, and whatever test
  the generator wrote. Treat the runner as compromised for the duration of the
  job. That is why it has nothing worth stealing.

`workflow_run` workflow (`.github/workflows/catchpr-comment.yml`):

- The workflow file that runs is the one on the default branch, not the file
  from the pull request.
- Checks out the default branch only, with `persist-credentials: false`.
- Downloads the artifact and rebuilds the comment from the JSON. It does not
  run pytest, does not import the pull request, and does not shell out to the
  generated test.
- The pull request number comes from `workflow_run.pull_requests`, not from
  the artifact. A forged artifact cannot choose a different pull request.
- If there is no validated catch, an existing sticky comment is deleted.

`pull_request_target` is not used. That event checks out untrusted code into a
workflow that still has the base repository's secrets. It is a known way to
turn a pull request into a secret-exfiltration bug. Do not add it.

## The artifact is data

The untrusted job can write whatever JSON it wants, because a pull request can
replace catchpr itself before the job runs. The comment job therefore:

- Parses JSON and ignores unknown fields.
- Renders test source and process output inside fenced code blocks.
- Inserts a zero-width space after `@` so a failure log cannot mention a user
  or a team.
- Caps the comment size.
- Never `eval`s the payload and never checks out `head_sha`.

## The local sandbox

Each test runs in its own process group, with a wall-clock timeout, `RLIMIT_CPU`,
`RLIMIT_AS`, `RLIMIT_FSIZE`, `RLIMIT_NOFILE`, and `RLIMIT_CORE`. On Linux, when
user namespaces allow it, the process is started under `unshare --net` so it has
no network interfaces. A `sitecustomize` guard also rejects `socket.connect` and
`socket.create_connection`, including on macOS where `unshare` does not exist.
`GITHUB_TOKEN`, `GH_TOKEN`, and environment variables ending in `_API_KEY` or
`_TOKEN` are removed from the child environment.

Generated tests are rejected before execution when they import `subprocess`,
`socket`, `ctypes`, `pty`, `pickle`, or `multiprocessing`, or when they call
`eval`, `exec`, `os.system`, or `os.popen`. This is a filter, not a boundary.
A determined test can still read files the runner user can read. In CI that is
acceptable only because the job has no secrets. Locally, do not point catchpr
at a repository you do not trust while your shell has credentials loaded.

`RLIMIT_NPROC` is intentionally not set. On Linux it accounts for every process
of the user, not the child tree, and a low value will break the runner itself.

## Model providers

`GroqProvider` and `OpenAICompatibleProvider` send the diff, which is untrusted
text, to an external API. A hostile diff can try to prompt-inject the model
into emitting a malicious test. The sandbox and the static import filter are
the mitigation, and they are partial.

Do not put `GROQ_API_KEY` or `OPENAI_API_KEY` in the `pull_request` job. The
untrusted code can read the environment. Run live models from a trusted
context (`python -m catchbench.live`, or a `workflow_dispatch` you start
yourself) or accept the mock provider in CI.

Keys are read from the environment or from a constructor argument. They are
not logged and they are not written into reports.

## Supply chain

Actions are pinned to full commit SHAs. Dependabot or a manual bump should
move those SHAs. There is no runtime dependency beyond `pytest`.

## Reporting a vulnerability

Open a private security advisory on the repository once it exists. Until then,
describe the issue without including a live exploit in a public issue. Please
include the catchpr version and whether the behavior is in the CLI, the
sandbox, or the GitHub Action.
