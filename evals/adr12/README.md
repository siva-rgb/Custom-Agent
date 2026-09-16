# The first ADR-12 comparison (M15)

This harness measures this SDK against the raw Claude Agent SDK on 12 file tasks, with the same
model through the same gateway, the same turn limit, and read-only tools over a copy of the
fixture folder. It lives outside the `agentsdk` package and never ships in the wheel (FR-61,
NFR-20): nothing under `agentsdk/` imports it or the Claude Agent SDK.

## What it measures

For every task, arm and repetition (FR-62):

- whether the task's deterministic checker passed, and the run's terminal status;
- turns, the token counts, and wall time;
- the cost, computed for **both** arms from one price list;
- the Claude Agent SDK's own cost estimate, recorded separately and labelled as its estimate.

A measure an arm does not report reads `unavailable`, never `0`. The report states what happened
and leaves the conclusions to the reader: it is an input to the second increment's specification
and to Phase 5A, not a verdict on either SDK.

## Setup

```bash
.venv/Scripts/python -m pip install -r evals/adr12/requirements.txt   # claude-agent-sdk, pinned
npm install --prefix evals/adr12                                      # the Claude Code CLI, pinned
```

The Claude Agent SDK drives the Claude Code CLI, so the CLI is installed here rather than
machine-wide, pinned in `package.json` and named in every report (DECISION-05ee16ad). A global
install would move under the comparison's feet.

## Running it

```bash
python evals/adr12/run_comparison.py --repetitions 3        # the whole set, live
python evals/adr12/run_comparison.py --only entry-count --repetitions 1   # one task, as a smoke run
```

Run it from the repository root: it reads `BASE_URL` and `MODEL_API_KEY` from the `.env` in the
folder you run it from, and refuses to start without them. Reports land in `reports/`, which is
deliberately **not** gitignored, so the credential scan reads what a run wrote (AC-50).

The offline half needs neither the gateway nor the CLI: `tests/test_adr12_comparison.py` runs the
whole task set with scripted stand-ins on both arms (AC-48), and that is what the gate collects.

## What is here

| file | what it holds |
|---|---|
| `tasks.py` | the 12 tasks and their checkers: single-step, multi-step, long, and one to decline |
| `fixture/` | the files every task reads; each run gets its own copy outside the repository |
| `arms.py` | the two arms, each taking its runner as an argument so the set runs offline |
| `harness.py` | runs every task on every arm, checks and costs each run |
| `report.py` | writes the JSON and the Markdown |
| `run_comparison.py` | the live command, the only thing here that spends tokens |
| `reports/` | where reports land; kept in the repository |

Web tasks are left out on purpose (FR-61): this SDK's fetch tool refuses loopback addresses, and
the Claude Agent SDK's WebFetch upgrades to HTTPS, sends the host name to Anthropic for a safety
check and summarises the page, so a web task would compare two different things.
