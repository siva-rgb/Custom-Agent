You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**Use a fresh model: this round is for Fable 5.1**, which last saw this code at round 2 and is the
least recent reviewer of it. M18 was implemented by Opus 5; rounds 1 and 3 were reviewed by
Sonnet 5 and round 2 by Fable 5.1, and **all three rejected it**. If you have reviewed this round
before, say so and ask for a different session. Round 3's defect was not in what the code does but
in how a fix was written -- a store call outside the guard that exists for exactly that -- so look
for invariants whose enforcement predates the call sites M18 adds. Re-derive the requirements from
SPEC.md rather than inheriting the framing below.

## Repository

`<repo>` is the folder that holds this file's repository; run everything from it.

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The PATH prefix is required: graphizer shells out to python3, and without the venv first on PATH
that hits the Microsoft Store alias and crashes Node.

- **Configuration:** both gates need DATABASE_URL from .env; the regression gate also needs
  BASE_URL and MODEL_API_KEY. **Never print credentials or a connection string.**
- **Install:** `.venv\Scripts\python.exe -m pip install -r requirements.txt`. M18 adds no library.
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

**M18-subagents**, **round 4**: the orchestrator spawns subagents as linked runs: hub-and-spoke
with curated briefing, taint carried across the run boundary, a structured result contract, and a
depth limit with the new `SchedulerLimits` fields.

Three rounds have rejected this milestone. The first two were about what a child's result said
about itself; the third was about how the fix for the second was written:

| round | reviewer | finding | decision |
|---|---|---|---|
| 1 | Sonnet 5 | L1: taint read from an input was dropped when a later input failed. L2: a child's manifest recorded the Runner's limits, not the pool's -- and `_limits_for` dropped M18's three limits for **every** run that set its own | DECISION-9672c566 |
| 2 | Fable 5.1 | M1: the label covered the briefing's inputs only, so a child that fetched a page through its own tool handed the text to its parent labelled clean | DECISION-302fc6ff |
| 3 | Sonnet 5 | N1: the fix for M1 read the child's history **on the event loop** -- a pooled connection and a SELECT after every completed child, while the pool held a concurrency slot. Third recurrence of KNOWLEDGE-c0f23fea, and invisible to the enforcement test that exists for it | DECISION-60a535b7 |

Read all three decisions. Round 3 is the one to learn from: the property was stated
(DECISION-6b62d1f5, "every store call made during a run goes through `asyncio.to_thread`") and
enforced by a test that drove five `Runner.run` paths and no pool spawn, so a new call site was
outside the guard. **Look for the same shape elsewhere**: a stated invariant whose enforcement
only visits the call sites that existed when it was written.

You are a different model from round 3; do not inherit its coverage or the earlier rounds'.

The requirements are FR-70, FR-71, FR-72, AC-56, AC-57 and NFR-22, quoted below. Decisions:
DECISION-35f4c3f4, P2-D21, DECISION-21e5d2e9, and the three above. Pre-flight: KNOWLEDGE-1f2f0e44.
D13 applies.

**M18 is not committed.** HEAD is `4cfb054` (M17). Review it with:

```bash
git diff 4cfb054 -- agentsdk tests scripts README.md
git status --short
```

### What changed since round 3

- **The read is offloaded.** `SubagentPool._what_it_read` is now a coroutine that awaits
  `asyncio.to_thread(self._runner._history_for, child)`.
- **The guard was extended, not just the code.** `test_no_store_call_runs_on_the_event_loop_thread_on_any_run_path`
  gains a sixth path: a `SubagentPool` spawn. A mutant that restores the direct call is killed by
  that test, which is the evidence the site is now inside the guard rather than merely fixed.
- Nothing else changed: rounds 1 and 2's fixes were confirmed closed by rerunning those rounds'
  own probes.

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

- `agentsdk/subagents.py`: the pool, the briefing, the result, `_what_it_read`.
- `agentsdk/api.py`: `_history_for`, `_limits_for`, `RunConfig.depth`, `RunConfig.output_schema`.
- `agentsdk/scheduler.py`, `agentsdk/loop.py`, `agentsdk/context.py`,
  `agentsdk/providers/openai_compatible.py`.
- `agentsdk/budget.py`, `agentsdk/handle.py`: M17's K3 and K1.
- `agentsdk/__init__.py`, `README.md`, `scripts/16_subagents.py`.
- `tests/test_subagents.py` (36 tests), `tests/test_phase2_readiness.py` (the extended guard),
  `tests/test_concurrency.py`, `tests/test_budget.py`, `tests/test_distribution.py`.

## What the author ran for round 4

1. **Confirmed the finding by thread identity**, then offloaded the read and extended the guard.
2. **Gate file:** 36 passed. **Full suite:** 1504 passed in 210.79 s.
3. **Demo command:** 7 of 7 offline and live.
4. **Mutation run:** **37 of 38 killed**, D11 declared; N1 (the un-offloaded read) is killed by the
   extended guard.
5. **Gates.** Both green on source hash `21cc3067`, the hash KICKOFF.md records, from one
   `genesis gate` run.
   - `unit`: `.venv\Scripts\python.exe -m pytest tests/test_subagents.py -q`, exit 0, 36 passed in 4.06s (12:45:03 to 12:45:08 UTC).
   - `regression`: `.venv\Scripts\python.exe -m pytest -q`, exit 0, 1504 passed in 263.31s (0:04:23)
     (12:45:08 to 12:49:33 UTC).

## Attack these first

**Report a result for every item, `probed: <what you ran and what it showed>` or `not probed:
<why>`.** Save probe scripts under

`C:\Users\<user>\AppData\Local\Temp\claude\<session-folder>\7dae5546-45c4-45ef-a67e-cb3e638507df\scratchpad\m18r4-probes\`

with shared cleanup in one `common.py`, and tenant ids starting `SYN-m18r4`. Earlier rounds'
probes are in `m18r1-probes\` through `m18r3-probes\`.

1. **Every store call M18 can reach, on every path**, by thread identity and not by clock:
   spawn, nested spawn, a failing child, a cancelled child, a refused spawn, the artifact reads in
   `_brief`, and anything the demo does. Is the sixth path in the guard the only one missing, or
   are there others?
2. **Other invariants whose enforcement predates M18's call sites.** AC-44's residue check,
   NFR-17's contiguity, FR-40's provenance on every result, the credential scan: does each one
   actually visit what M18 added?
3. **Can anything still cross with a label cleaner than its source**, including a grandchild's
   taint reaching a grandparent, and a tool whose provenance a hook relabels?
4. **What every run records about itself**, children and non-children alike.
5. **The structured contract** end to end, with a real provider and a stubbed one.
6. **The limits** under real parallelism, per parent, and nested (see M5 below).
7. **The demo command**, checked against the stores rather than its printout.

## Declared limitations: known, recorded, NOT findings

- **D11 survives:** `RunConfig.depth` is written by the pool and read by nothing until M19; round 1
  verified that recording it in the `RunStarted` payload breaks an approved M14 test.
- **Failing a node whose history cannot be read** is deliberate: a label this pool cannot justify
  is worse than no answer. Rounds 2 and 3 both agreed; the un-offloaded read that made it worse is
  fixed.
- **Round 2's caveats:** M3 (the manifest cannot tell a structured child from a plain one), **M4**
  (a pool's limits replace the Runner's per-tool limits for children: recorded consistently,
  widened silently), **M5** (limits counted per parent scope, so a nested tree can reach N + N² at
  once), M6 (an invalid JSON Schema is reported as the model's failure), M8
  (`SubagentResult.artifacts` stays empty until tools get a run scope, FR-76 in M19).
- **Round 3's notes:** a tool wrapping a nested `spawn()` cannot pass the grandchild's real
  provenance -- an M19 gap, since nothing in M18 wires a tool to the pool.
- **Round 1's caveats:** the adapter's refusal check is a substring match; depth and parent are
  caller-trusted with no ancestry check (D13); `principal_context` is passed, not inherited.
- **A weak assertion, still recorded:** round 1 changed an approved M11 test to compare the stored
  limits against `to_json()` itself, so a key dropped from `to_json` would vanish from both sides.
- **M17's K4 to K8** remain recorded and unfixed (KNOWLEDGE-fdff16dc).
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

