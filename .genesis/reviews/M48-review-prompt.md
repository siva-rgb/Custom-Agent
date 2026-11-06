You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**This is M48, round 1. Use a fresh session: Fable 5.1, falling back to Opus 5** if Fable 5.1 fails
to start. M48 was implemented by Opus 5.5 and its adversarial pre-review run by a Sonnet 5.5 subagent.
Bring no memory of an earlier review into this one: if your session holds any, say so and ask for a
fresh one.

M48 opens **Phase 5A** (a general agent built on the SDK's governance, DECISION-dd87feb8). It gives a
plain top-level run a budget of its own: `RunConfig.budget`, a `BudgetPolicy`, stops the run
`budget_exceeded` at its ceiling, model calls and compute alike, closing backlog I-04.

**The owner's rules.** Every issue is fixed in the milestone that found it; nothing carries to the
next. And the review stopping rule (DECISION-c697f36c): a round that approves with nothing blocking is
the last; its findings are fixed without another round. So report everything you find, and say
clearly which you would block on.

The requirements are quoted below **verbatim from SPEC.md as approved**, copied by a script. Check
each quote against SPEC.md yourself; where this brief and SPEC.md differ, SPEC.md wins and the
difference is a finding.

## The requirements, verbatim

### From the decisions table (P5A-D8)

| Decision | P5A-D1 to P5A-D14 |
|---|---|
| P5A-D8 Budget | Budgets for plain runs (I-04): a single run takes a `BudgetPolicy` and is stopped `budget_exceeded` at its ceiling, model calls and compute alike. The preset always has one |

### "#### M48: budgets for plain runs"

- FR-172: A top-level run can be given a budget (P5A-D8, closing backlog I-04).
  - **`RunConfig.budget`**, a `BudgetPolicy` or None (the default).
  - **Mechanism:** the `Runner` turns it into a lease of a single-run governor, `BudgetGovernor.for_single_run(policy)`, whose one lease holds the whole ceiling, in USD, tokens or both -- no reserve is set aside, since no replan or sibling can claim it. The lease is passed where `budget_lease` is today, so the loop's check before each model call (FR-68) and `run_command`'s before each command (FR-157) are unchanged; each model call's and each command's cost is charged when it ends.
  - **The end:** a run whose ceiling is spent ends `failed`, reason `budget_exceeded`, after the call or command that crossed it is recorded; the overshoot is at most that one call or command, as for a child. A run whose final answer is the call that crossed its ceiling completes, its spend over the ceiling recorded, as a child does (DECISION-6a07dbc6).
  - **Refused before the run starts:** `ValueError` for a budget beside a `budget_lease`, on a run the `SubagentPool` places, or on an orchestrator's run, which has its own (FR-67); `UnpricedModel` for a USD ceiling on a model nothing prices (FR-69). A token ceiling applies whatever the prices.
  - **Recorded:** the manifest's `budget_policy`, and `budget_reservations` naming the run's one lease; the run's spend in its `runs` row and its `RunResult`, as a child's is.
- FR-173: **Honest spend for a plain run** (NFR-23): its recorded spend equals the sum of its `ModelCalled` and `WorkspaceCommandEnded` costs. A command's cost is None only when `cpu_second_usd` is None, and is then charged nothing (FR-157). A plain run with no budget records exactly what it recorded before M48 (NFR-50).

### Non-functional

- NFR-50: A run with no `budget`, no preset and no workspace `files` sends the same requests and records the same events, rows and manifest as before M48, apart from the manifest's `preset` column, NULL (FR-176); a workspace spec without `files` and with the default `export_approval` has the record it had; an orchestrator without `role_turns` briefs every child with the turns it did (FR-172, FR-174, FR-175, FR-177).

### Acceptance

- AC-106: FR-172 and FR-173, on both stores: a plain run with a USD ceiling, and one with a token ceiling, each ends `budget_exceeded` after the model call that crossed it is recorded, unless that call was its final answer, when it completes with its spend recorded; one crossed by a command's compute ends the same way, the command recorded; a budget beside a lease, on a pool-placed child or on an orchestrator's run raises `ValueError`, and a USD ceiling on an unpriced model raises `UnpricedModel`, each before the run starts; a run without a budget records what it did before M48.

## The records

All in `.genesis/project.json`. A choice they record is not a defect; code that differs from them is.
- **DECISION-e66f1af4, DECISION-2cdd97da, DECISION-abd83872:** Phase 5A's decisions P5A-D1 to
  P5A-D14, and the spec review's amendments.
- **DECISION-6a07dbc6** (pre-flight): `RunConfig.budget` becomes, once at the start of the run, the one
  lease of `BudgetGovernor.for_single_run(policy)`, its reservation named `run`; refusals read existing
  fields; no migration. **Owner:** a run whose final answer is the call that crossed its ceiling
  completes, its overspend recorded, as a plan's child does.
- **DECISION-23563bf6** (the pre-review's findings, below).
- Standing: KNOWLEDGE-1afcf6b2 (no vacuous check), DECISION-94bfee7c (no check keys on an error's
  text), NFR-11 and NFR-23 (an unknown cost stays None; spend is honest).

## Repository

`<repo>` is the folder that holds this file's repository; run everything from it. The branch is
`phase-5a`; HEAD is `711088f` (M47; `main` points there too). M48 and Phase 5A's SPEC section are
uncommitted. M48 adds no migration and no dependency.

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The PATH prefix is required. **Never pass an unrecognised flag to a genesis command:** `--help`,
`--check` and `--dry-run` are all swallowed as arguments and the command runs. **Do not record
anything with genesis while a gate runs.**

- **Configuration:** DATABASE_URL from .env. **Never print credentials or a connection string;** on a
  database error print the exception's type only.
- **Host:** Windows 11 Home, Python 3.11. **This laptop sleeps when idle:** run anything over a few
  minutes under a wrapper holding `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)`. That
  does not stop a sleep the owner starts (lid, button): before calling any delay real, compare wall
  time with monotonic time, or read the Kernel-Power events (506/507).
- **Docker Desktop is running.** **Never pull, build, prune, stop or restart anything.** Touch only
  what carries your own `agentsdk.tenant` prefix. Report how many remain at the end (it must be 0).
- **Line endings vary by file** (some LF, most CRLF). A mutant whose edit silently never applies looks
  exactly like a surviving one: assert the file changed before running its test, and restore it byte
  for byte, verified by SHA-256. **The same holds for a runtime patch: count how often it fired, and
  treat zero as no result.**

**Running tests**
- **One test session at a time.** Check that no other python or node process of yours is running
  first. Three python processes (PIDs 27472, 6348, 17824) are the owner's Jupyter kernel: leave them.
  Run with `-p no:cacheprovider`; `pytest.ini` deselects Docker tests unless you pass `-m docker`.
  **Do not kill a gate mid-run.**
- **Gates:** `genesis gate . M48-plain-run-budgets --timeout 7200000` in the background, kept awake:
  unit (`tests/test_plain_budgets.py`), regression (the full suite, about 37 minutes), and `docker`
  (every Phase 5 Docker-marked test plus M48's, about 77 minutes).
- M48's own tests: `pytest tests/test_plain_budgets.py -p no:cacheprovider`, seconds; its Docker test
  with `-m docker`, seconds.

**Cleanup:** `workspace_commands`, `workspaces`, then `run_events`, `messages`, `execution_manifests`
by run id, then the runs. Use tenant ids starting `SYN-m48r1`, and remove only rows your probes
recorded.

## Task under review

### What M48 builds

- **`agentsdk/budget.py`:** `BudgetGovernor.for_single_run(policy)` and `SINGLE_RUN = "run"`: one
  reservation holding the whole ceiling, no orchestrator reserve, nothing unallocated.
- **`agentsdk/api.py`:** `RunConfig.budget`; `Runner._budgeted(config)`, called first in
  `Runner.start` (which `run` calls), refusing a budget beside a `budget_lease` or on a pool-placed run
  (`depth` set) and returning the config with the new lease as its `budget_lease`. Everything after it
  is the existing lease path: `_open`'s `UnpricedModel` refusal, the loop's check before each model
  call, `_Compute.may_run` before each command, the charges, the manifest's budget columns.
- **`agentsdk/orchestrator.py`:** `run_plan` refuses any lease but the orchestrator's own (pre-review
  F4).
- **README.md:** a paragraph on plain-run budgets, and a stale row that said budgets are unbuilt.

### The pre-review's findings, answered (DECISION-23563bf6)

A fresh Sonnet 5.5 subagent probed M48 at runtime and found nothing blocking. Each finding was
verified by the author first-hand; each repair has a mutant in the appendix that a test kills.

| Finding | Repair |
|---|---|
| F1: no test held an unknown cost (a mutant charging None as 0 survived). | Tests: a call nothing prices leaves the spend None on both stores, and under a USD ceiling stops the run. |
| F2: no test pinned a second command of the crossing turn. | A test: it never starts; overshoot one command. |
| F3: FR-172 and AC-106 said a crossed run ends `budget_exceeded`, against DECISION-6a07dbc6. | **Owner:** one sentence each; M48 requeued, then spec and plan re-approved under the owner. |
| F4: an orchestrator's agent run with `RunConfig(budget=...)` failed after a paid planning call, with a misleading message. | `run_plan` refuses any lease but the orchestrator's own, with its existing message. |

The pre-review also proved NFR-50 by running one scripted run on HEAD's package and on the working
tree and comparing events, results and rows: equal. And it left a forward note, not an M48 item: an
application's own `SubagentPool.spawn` from a plain run passes no lease, so increment 2's delegation
tool must pass the run's.

### How the author checked it

- **Tests:** `tests/test_plain_budgets.py`, 30 tests: 29 offline (on both stores where a store matters) and 1 Docker-marked (a real busy loop crossing a compute ceiling). Written before the code and run against it first (23 of 23 failed on the
  missing field); the pre-review's tests were added after.
- **Mutation:** 11 mutants, one exact edit each, every file restored byte for byte by
  SHA-256 (appendix). A first run's survivor showed `_budgeted`'s own type check duplicated the
  governor's; it was removed.
- **Older suites:** every test file touching the orchestrator or budgets, 805 passed before the gates.
- **Gates** on source hash `033e46a94a9bbac10b904b80ac8ebddb06292e7caa0cf4094c2bee2a56491e9a`: unit 29 passed, 1 deselected in 8.91s; regression 3330 passed, 169 deselected in 1929.57s (0:32:09); docker 169 passed, 768 deselected in 4332.87s (1:12:12), each exit 0 with empty stderr and no warnings, kept awake. A first run passed too, but its hash included a
  stray probe (`p2.py`) the pre-reviewer had left at the repository root despite reporting no repo
  edit; nothing imported it and pytest never collected it. The owner chose to delete it and rerun the
  gates, so the evidence hashes exactly the tree under review. Check `git status --untracked-files=all`
  yourself.

### Edits to approved tests

None: `tests/test_plain_budgets.py` is new, and no other test file changed (check with
`git status --short tests/`).

## Attack these first

Report each as "probed: <result>" or "not probed: <why>".

1. **Bypass.** Any path where a budget is ignored or a second governor appears: `Runner.start` and
   `run`, a reused or replaced `RunConfig`, `Orchestrator.config(**options)`, an FR-21 child built by
   application code, approval waits, cancellation, parallel tool calls, compaction's summary call.
2. **Isolation.** Can one run's spend stop another, through a shared policy, config or anything else?
3. **Accounting.** On both stores: `RunResult.cost_usd`, `runs.cost_usd` and the manifest's
   `budget_policy`, `budget_reservations` and `budget_spend` against the sum of `ModelCalled` and
   `WorkspaceCommandEnded` costs; an unknown cost never 0; compute charged only with `cpu_second_usd`.
4. **The end.** `budget_exceeded` before a further call or command; a final answer on the crossing
   call completes; overshoot at most one call or one command, several commands in one turn included.
5. **Refusals.** Each raises before anything is recorded, with no model call made.
6. **NFR-50.** A run with no budget sends the same requests and records the same events, rows and
   manifest as at HEAD.
7. **The plan tool.** Does F4's refusal stop anything an orchestrator run legitimately does?
8. **Tests.** Can any pass when its subject never happened? Break the code at runtime and watch each
   fail.

## Declared limitations: known, recorded, NOT findings

- **Everything declared for M20 to M47,** and the README's rows.
- **Owner's choices:** the decisions above; soft enforcement (one call or command of overshoot), as
  for every lease since M17.
- **Tests read private state:** `Runner._workspace_store`, `Orchestrator._run_plan` with a stand-in
  scope.

## Rules

- **Do not fix the code.** Report defects; the implementing session repairs them.
- **Gates are computed, never narrated.** Paste real command output for anything you assert.
- **Approve if it is sound.** A defect must be reachable and must matter. List every finding, and mark
  which you would block on.
- **Save every probe script** under a folder of your own in the system temp directory, and say where.
- **Clean up** every run, row, container, volume, network, folder, process and lock you create, and say
  what you removed.

## Recording your decision

Use **single quotes** around `--reason`, and keep apostrophes, backticks and `$` out of it.

```bash
# pass
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control approve . M48-plain-run-budgets --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M48-plain-run-budgets --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, the result for each attack item, and your verdict. Do
not commit or publish anything.

## Appendix: the author's mutants

- the budget never becomes a lease
- a budget ignored once checked
- a budget beside a lease allowed
- a budget on a pool-placed child allowed
- a reserve set aside from the single run's ceiling
- the single run's token ceiling dropped
- the governor's own type check removed
- F4: the plan tool takes any lease
- F1: an unknown cost charged as zero
- F2: every command may start
- the reservation renamed
