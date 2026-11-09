# The ADR-12 comparison (M15, grown in M50, tuned and rerun in M51)

This harness measures this SDK against the raw Claude Agent SDK, with the same model through the
same gateway and the same turn limit. It lives outside the `agentsdk` package and never ships in
the wheel (FR-61, NFR-20, NFR-51): nothing under `agentsdk/` imports it, the Claude Agent SDK, or
the Claude arm's image.

It holds two task sets (FR-179, DECISION-d0269633):

- **The 12 M15 tasks** -- single-step, multi-step, long, and one to decline -- run as M15 ran them:
  read-only file tools over a host copy of the fixture folder, checked by the answer text.
- **The 24 sandbox tasks** -- shell work, code that must run and pass the task's own `check.py`,
  files written and edited, a fetch through the workspace's egress to `example.com:443`, and
  multi-step repairs -- each checked by the workspace it leaves: its files and what the run's own
  programs wrote, not the answer text alone.

**How code and fetches are judged** (DECISION-b944d3ab). A run's own `check.txt` is only its
claim. After a code or repair task, the harness copies the files read back, with a pristine
verifier, into a fresh container of the workspace image with no network and no capabilities; each
case -- the visible ones and hidden ones the run never saw -- loads the named module file in a
child process, and the verifier's result carries a nonce no workspace code sees. A fetch task is
judged against the page the harness fetches itself from the same URL once per comparison, its
time, length and SHA-256 named in the report, and passes only with evidence the run reached the
host (DECISION-7830ca10), kept in each row's `fetch_evidence`: on this SDK's arm an allowed
connection its workspace's proxy recorded, by id; on the Claude arm, whose network is not recorded,
a call whose result was not an error, read as the shell would run it (`fetch_proof.py`): `WebFetch`
of the host's URL, or a `Bash` command whose program, after any wrapper such as `timeout`, is `curl`
or `wget` with the host among its arguments, or `python` or `node` running code that uses an HTTP
client and names the host's URL, given inline, in a heredoc, or in a script the run wrote. `echo`,
a comment, quoted text, a failed call, and a page reproduced from memory, however exact, are no
fetch. The Claude arm's evidence is the command the run gave, not the network: a URL computed at
run time, or a script edited after it was written, is a fetch it does not count.

On a sandbox task, **this SDK's arm** runs the general preset (FR-174) in a `DockerProvider`
workspace holding the task's files, its egress the task's allow list, `keep=True`, tenant `adr12`
and project `comparison`. Every export is refused at once, and each refusal is recorded. The kept
workspace is read back through the provider's `export`, parsed in memory, and released.
**The Claude arm** runs the Python `claude_agent_sdk` and the pinned CLI inside a disposable
container from the image this folder's `Dockerfile` builds: the task's files are copied in, tools
`Read`, `Glob`, `Grep`, `Bash`, `Write` and `Edit`, the working folder read back the same way, and
the container removed. It runs with ordinary networking, to reach the gateway and the fetch
tasks' host, while this SDK's arm reaches only its allow list; the report states that asymmetry.

## What it measures

For every task, arm and repetition (FR-62, FR-179):

- whether the task's deterministic checker passed, and the run's terminal status --
  `budget_exceeded` reported as a status of its own;
- turns, the token counts, and wall time;
- the cost, computed for **both** arms from one price list;
- the Claude Agent SDK's own cost estimate, recorded separately and labelled as its estimate;
- on a sandbox task, the files read back, the hosts the run is shown to have reached and, on this
  SDK's arm, the exports refused.

The report sums these per kind and overall, for each arm: pass rate, terminal statuses, median
turns, tokens, cost and median wall time (FR-181).

**Budgets.** This SDK's arm runs every task within one rule per kind: a USD and a token ceiling of
ten times that kind's median on this SDK's arm in M15 (`reports/adr12-comparison-2026-09-16.json`);
the sandbox kind takes ten times the multi-step median, $0.023940 and 20,175 tokens. The Claude
arm has no budget. The report names the rule and each kind's ceilings.

A measure an arm does not report reads `unavailable`, never `0`. The report states what happened
and leaves the conclusions to the reader: it is an input to Phase 5A, not a verdict on either SDK.

## Setup

```bash
.venv/Scripts/python -m pip install -r evals/adr12/requirements.txt   # claude-agent-sdk, pinned
npm install --prefix evals/adr12                                      # the Claude Code CLI, pinned
docker build -t adr12-claude:cli-2.1.267 evals/adr12                  # the Claude arm's image (owner)
```

The Claude Agent SDK drives the Claude Code CLI, so the CLI is installed here rather than
machine-wide, pinned in `package.json` and named in every report (DECISION-05ee16ad). The image is
built by the owner, or with the owner's approval, from the same Python base as this SDK's
workspace image; nothing in a gate or in the live command pulls or builds it, and it holds no
credential: the gateway's key reaches only a container's environment, named on its `docker
create` line and never given its value there.

## Running it

```bash
python evals/adr12/run_comparison.py --repetitions 3                    # every task, live
python evals/adr12/run_comparison.py --set sandbox --repetitions 1      # the sandbox tasks only
python evals/adr12/run_comparison.py --only fetch-page --repetitions 1  # one task, as a smoke run
```

Run it from the repository root: it reads `BASE_URL` and `MODEL_API_KEY` from the `.env` in the
folder you run it from, and refuses to start without them. The sandbox tasks need Docker and the
built image. Reports land in `reports/`, which is deliberately **not** gitignored, so the
credential scan reads what a run wrote (AC-50).

## Tuning the preset

```bash
python evals/adr12/tune.py --model bedrock.anthropic.claude-haiku-4-5
python evals/adr12/tune.py --model openai.gpt-4o-mini
```

Tuning changes the general preset's instructions alone, between comparisons (FR-182). Each change
is a new version in `agentsdk/presets.py` and a new `agent_spec_hash`. `tune.py` runs the 24
sandbox tasks once on this SDK's arm with the version as shipped, on one of two upstream providers
through the gateway, keeps the run's full report under `reports/tuning/`, and appends it to
`reports/tuning/tuning-log.json` with the version, its instructions' hash and each task's result;
`tuning-log.md` shows each version against its predecessor. Tuning stops at the first version that
passes, on both providers, every task its predecessor passed, or at the third version after
`general-v1`; the final comparison runs the last version (DECISION-7830ca10).

The offline half needs neither the gateway nor the CLI: `tests/test_adr12_comparison.py` runs the
M15 tasks with scripted stand-ins on both arms (AC-48), and `tests/test_adr12_sandbox.py` holds
every sandbox checker against its good, bad and tricky outcomes, runs each task's own check, and
runs both sandbox arms with stand-ins (FR-180). Its docker-marked tests run this SDK's arm on
`DockerProvider` and the Claude arm's container on the built image. `tests/test_adr12_tuning.py`
holds the tuning log and its stop rule. Only `run_comparison.py` and `tune.py` spend tokens.

## What is here

| file | what it holds |
|---|---|
| `tasks.py` | the 12 M15 tasks and their checkers |
| `fixture/` | the files every M15 task reads; each run gets its own copy outside the repository |
| `sandbox.py` | the 24 sandbox tasks, their files and checkers, and the budget rule |
| `arms.py` | the M15 arms, each taking its runner as an argument so the set runs offline |
| `sandbox_arms.py` | the sandbox arms: the general preset, and the Claude Agent SDK in its container |
| `fetch_proof.py` | the Claude arm's evidence of a fetch: its calls, read as the shell would run them |
| `claude_driver.py` | the program the Claude arm's container runs |
| `Dockerfile`, `.dockerignore` | the Claude arm's image |
| `harness.py` | runs every task on every arm, checks and costs each run |
| `report.py` | writes the JSON and the Markdown |
| `run_comparison.py` | the live comparison |
| `tune.py` | the live tuning runs, and the tuning log and its stop rule |
| `reports/` | where reports land; kept in the repository |

The M15 tasks name no URL (FR-61): this SDK's fetch tool refuses loopback addresses, and the Claude
Agent SDK's WebFetch summarises a page instead of reading it. The sandbox fetch tasks avoid both:
each arm fetches with whatever program it writes, in its own sandbox, and the checker reads the
saved page.
