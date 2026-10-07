You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**Use a fresh model: this round is for Fable 5.1.** M18 was implemented by Opus 5 and round 1 was
reviewed by Sonnet 5, which rejected it. If you have reviewed M18 before, say so and ask for a
different session. M18 hands one run's work to another, so what to distrust is what crosses that
boundary and what each run leaves behind about itself: round 1 found one wrong provenance and one
manifest describing limits that never governed the run. Re-derive the requirements from SPEC.md
rather than inheriting the framing below.

## Repository

`<repo>` is the folder that holds this file's repository; run everything from it.

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The PATH prefix is required: graphizer shells out to python3, and without the venv first on PATH
that hits the Microsoft Store alias and crashes Node.

- **Configuration:** both gates need DATABASE_URL from .env; the regression gate also needs
  BASE_URL and MODEL_API_KEY. **Never print credentials or a connection string.**
- **Install:** `.venv\Scripts\python.exe -m pip install -r requirements.txt`. M16 adds no library.
- **Host:** Windows Developer Mode must be on (D7).

**Only one test session at a time, and nothing else in parallel with one.** Every pytest session
compares the store's ids at its start and end (AC-44, now including `plan_versions` and
`plan_node_states`), the development database is shared, and M14's timing tests assert lower
bounds. Before running tests or gates, check that no other python or node process is running. Run
your own probes with `-p no:cacheprovider`: Genesis hashes .pytest_cache, so a cache write can make
a gate read stale with no code change. The full suite takes about 4 minutes; keep every command in
the foreground and under 590 s rather than backgrounding it and waiting.

**If the suite hangs, it is not memory.** Measured on 2026-09-19 (KNOWLEDGE-9a3abf61): the suite
peaks at 32 MB and passed with 924 MB free. Suspect the two live-gateway tests in
`test_golden_eval.py`, whose calls have no end-to-end deadline; rerun with
`-o faulthandler_timeout=120` to see where it stopped.

## Task under review

**M18-subagents**, **round 2**: the orchestrator spawns subagents as linked runs: hub-and-spoke
with curated briefing, taint carried across the run boundary, a structured result contract, and a
depth limit with the new `SchedulerLimits` fields.

Round 1 (Sonnet 5, 2026-09-29T07:48:53Z) rejected it on two findings, both reproduced by the
owner and both fixed here (**DECISION-9672c566**, read it first):

- **L1:** when a briefing named several inputs and an earlier one resolved as untrusted while a
  later one failed, the result reported `TRUSTED_SOURCE` and untainted: the taint already read was
  thrown away. No tainted text crossed, because such a result is failed with no output, but the
  recorded provenance was wrong on a path as ordinary as one stale artifact id.
- **L2:** a child's manifest recorded the Runner's default limits rather than the pool's, which
  are what actually bounded the fan-out. Two separate omissions caused it, and the second one
  affected **every** run that sets its own limits, not only children.

Your job is to judge the fixed code and M18 as a whole; you are a different model from round 1 and
should not inherit its coverage.

The requirements are FR-70, FR-71, FR-72, AC-56, AC-57 and NFR-22, quoted below. Decisions:
DECISION-35f4c3f4 (the pre-flight's four clarifications), P2-D21, DECISION-21e5d2e9 (M17's K1 and
K3 fixed here), DECISION-9672c566 (round 1). Pre-flight: KNOWLEDGE-1f2f0e44. D13 applies.

**M18 is not committed.** HEAD is `4cfb054` (M17). Review it with:

```bash
git diff 4cfb054 -- agentsdk tests scripts README.md
git status --short
```

### What changed since round 1

- **L1.** The provenances are gathered into a list the failure handler can see, so a result
  reports whatever had already been taken in, whatever happened next. Both orders are tested: a
  taint read before the failure is kept, and a failure before anything was read stays clean.
- **L2, two fixes.** The pool now sets `scheduler_limits` on the child's `RunConfig`, with the
  provider limits stripped because FR-43 forbids a `RunConfig` from carrying them. And
  `Runner._limits_for` rebuilt a run's own limits from three fields, so the three M18 added fell
  back to defaults: it now carries every field. **That second one was not a subagent bug** -- any
  run whose `RunConfig` set limits recorded the wrong subagent limits.
- **Tests:** 3 added (28 to 31), each seen failing first; mutants L1a, L1b, L2a and L2b added, and
  S4's snippet disambiguated because the fix gave it a second match.

### Requirements it claims to satisfy (verbatim from SPEC.md)

- **FR-70:** The orchestrator spawns a subagent as its own run, linked by the existing `runs.parent_run_id` (migration `0002`) and `RunStarted.parent_run_id` (FR-57), inheriting the parent's `tenant_id`, `project_id` and `PrincipalContext`. Topology is hub-and-spoke: a child is briefed only by its parent and returns only to its parent, and children never address one another. The briefing is curated, not inherited wholesale: a child receives its node's objective, its `input_refs` resolved through the run's artifact store, and nothing else of the parent's history. Every result a child returns carries the provenance and taint of its inputs, at the maximum taint of them (Phase 0's propagation rule), so a tainted child result cannot launder itself by crossing a run boundary.
- **FR-71:** A node with an `expected_output_schema` populates `ModelRequest.output_schema` for its child, the first use of that field. A result that does not validate is re-asked once, with the validation error appended to the child's history, and if the second result also fails the node ends `failed` with reason `output_contract_violation` (P2-D22). Both attempts draw on that node's reservation, so a model that cannot produce the schema cannot escape the budget by failing repeatedly. The invalid text is kept in the child's history and in its stored messages, so the failure is inspectable rather than merely reported.
- **FR-72:** `SchedulerLimits` gains `max_concurrent_subagents` (default 4), `max_tasks_per_run` (default 50) and `queue_policy` (`fifo`, the only value this increment accepts), added now rather than reserved in M11 (FR-43), and validated the same way. Recursion is bounded at depth 3 (P2-D21): the orchestrator is depth 0, and a spawn at depth 3 is refused with `MaxDepthExceeded`, a new error in the taxonomy, which fails that node without cancelling its siblings. A run that would exceed `max_tasks_per_run` refuses the spawn the same way. `scripts/16_subagents.py` fans out three children under a limit of 2 and prints their parent link, the peak concurrency and one refused spawn at depth 3.
- **NFR-22:** Orchestration never corrupts the record. Under parallel subagents, replanning and cancellation, every run's `sequence_no` values stay unique and contiguous on both stores, no child is left `running` once its parent has returned or raised, every child's `parent_run_id` resolves to a run that exists, and a plan version is never mutated after it is stored (NFR-17).
- **AC-56:** A child spawned at depth 3 is refused with `MaxDepthExceeded`, failing that node while its siblings finish; a nested child's reservation is drawn from its parent's, not from the run pool; and a run that would pass `max_tasks_per_run` refuses the spawn the same way.
- **AC-57:** A child whose result does not validate against its node's `expected_output_schema` is re-asked exactly once with the validation error in its history; a second invalid result ends the node `failed` with reason `output_contract_violation`; both attempts are charged to that node's reservation; and the invalid text is readable in the child's stored messages afterwards.

### Standing invariants that constrain every task

- Every `ToolResult` carries exactly one `ContentProvenance`.
- Every persisted row carries non-null `tenant_id` and `project_id`.
- `ToolExecutor` order is fixed: resolve, validate, permission, execute.
- No credential enters model context, a persisted row, or a `RunEvent` payload.
- Application code calls `Runner.run()` and nothing else.
- Only `AgentSDKError` subclasses may escape `ModelClient.send()`.
- A boundary's error path must not itself be able to raise.
- A cancellation is recorded, then re-raised (INVARIANT-73299c7c).

### Files in scope

- `agentsdk/subagents.py`: the pool, the briefing, the result, and both L1 and L2a.
- `agentsdk/api.py`: `_limits_for` (L2b), `RunConfig.depth` and `RunConfig.output_schema`.
- `agentsdk/scheduler.py`, `agentsdk/loop.py`, `agentsdk/context.py`,
  `agentsdk/providers/openai_compatible.py` (the schema in words and in `response_format`).
- `agentsdk/budget.py` and `agentsdk/handle.py`: M17's K3 and K1.
- `agentsdk/__init__.py`, `README.md`, `scripts/16_subagents.py`, `tests/test_distribution.py`,
  `tests/test_concurrency.py` (see the round 1 brief's A3), `tests/test_budget.py`.
- `tests/test_subagents.py` (31 tests).

## What the author ran for round 2

1. **Reproduced both findings first**, then wrote the three tests, which failed on the round 1
   code.
2. **Gate file:** 31 passed. **Full suite:** 1499 passed in 263.93 s.
3. **Demo command:** 7 of 7 offline and live.
4. **Mutation run**, files restored and SHA-256 verified: **32 of 33 killed**, D11 still declared
   (below).
5. **Gates.** Both green on source hash `96a80385`, the hash KICKOFF.md records, from one
   `genesis gate` run.
   - `unit`: `.venv\Scripts\python.exe -m pytest tests/test_subagents.py -q`, exit 0, 31 passed in 5.55s (08:27:57 to 08:28:04 UTC).
   - `regression`: `.venv\Scripts\python.exe -m pytest -q`, exit 0, 1499 passed in 256.07s (0:04:16)
     (08:28:04 to 08:32:21 UTC).

## Attack these first

**Report a result for every item, `probed: <what you ran and what it showed>` or `not probed:
<why>`.** Save probe scripts under

`C:\Users\<user>\AppData\Local\Temp\claude\<session-folder>\7dae5546-45c4-45ef-a67e-cb3e638507df\scratchpad\m18r2-probes\`

with shared cleanup in one `common.py`, and tenant ids starting `SYN-m18r2`. Round 1's probes are
in `m18r1-probes\` beside it.

1. **Provenance on every path.** L1 was one branch that built it from nothing. Find any other:
   a child that fails mid-run, a cancelled child, an empty briefing, an input whose metadata reads
   but whose content does not, duplicated inputs, an input resolved twice. Is the reported
   provenance ever cleaner than what was read?
2. **What is recorded about a child.** L2 was a manifest telling a story that never happened.
   Check every column a child writes -- limits, pricing, budget, parent, scope, principal context
   -- against what actually governed that run, and check runs that are **not** children too, since
   L2b touched them.
3. **The structured contract** end to end, with a real provider and a stubbed one: the schema in
   the instructions, `response_format`, the fallback, `max_turns`, and both attempts charged.
4. **The limits** under real parallelism, per parent, and whether a refused spawn leaves the pool
   usable.
5. **The curated briefing**: nothing of the parent reaching the child, everything of the node
   reaching it.
6. **NFR-22 and AC-44** after a fan-out, and nothing left running.
7. **The demo command**, checked against the stores rather than its printout.

## Declared limitations: known, recorded, NOT findings

- **D11 survives:** `RunConfig.depth` is written by the pool and read by nothing until M19. Round
  1 verified this, and verified that recording it in the `RunStarted` payload breaks an approved
  M14 test, so it stays unread rather than perturbing that test.
- **Round 1's three caveats**, accepted as they stand: the adapter's refusal check is a substring
  match, so an unrelated 400 naming `response_format` costs one wasted retry while the final error
  is still correct; depth and parent are caller-trusted with no ancestry check (D13); and
  `principal_context` is passed by the caller rather than inherited automatically.
- **Nothing drives the pool yet**: M19's orchestrator is its intended caller.
- **M17's K4 to K8** remain recorded and unfixed (KNOWLEDGE-fdff16dc).
- **An approved M11 test was changed** to follow the limits type rather than a three-key snapshot;
  see the round 1 brief's A3.
- **Older `independent-review` gates** compute stale because the repository hash moved.

## Rules

- **Do not fix the code.** Report defects; the implementing session repairs them.
- **Gates are computed, never narrated.** Paste real command output for anything you assert.
- **Approve if it is sound.** A defect must be reachable and must matter. Latent, out-of-scope or
  cosmetic findings are caveats in your reason, not blockers.
- **Restore every file you mutate** and verify SHA-256, restoring in a `finally`; kill the whole
  process tree on a timeout (`taskkill /T`). M18 adds no migration.
- **Clean up** every run, row, temp folder and process you create, and say what you removed.
- **Report every probe**, including ones that showed nothing.

## Recording your decision

Use **single quotes** around `--reason`, and keep apostrophes, backticks and `$` out of it.

```bash
# pass
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control approve . M18-subagents --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M18-subagents --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, the result for each of the seven attack items, and
your verdict.

