You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**Use a fresh model: this round is for Sonnet 5**, which last saw this code at round 1. M18 was
implemented by Opus 5; rounds 1 and 2 were reviewed by Sonnet 5 and Fable 5.1, and **both rejected
it**, each time because of what a run said about itself rather than what it did. If you have
reviewed this round before, say so and ask for a different session. Check labels against sources
and rows against events, first-hand. Re-derive the requirements from SPEC.md rather than
inheriting the framing below.

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

**M18-subagents**, **round 3**: the orchestrator spawns subagents as linked runs: hub-and-spoke
with curated briefing, taint carried across the run boundary, a structured result contract, and a
depth limit with the new `SchedulerLimits` fields.

Two rounds have rejected this milestone, both on what a child's result **says about itself**:

| round | reviewer | finding | decision |
|---|---|---|---|
| 1 | Sonnet 5 | L1: taint read from an input was dropped when a later input failed, so a failed result claimed `TRUSTED_SOURCE`. L2: a child's manifest recorded the Runner's default limits, not the pool's -- and behind it, `Runner._limits_for` dropped M18's three limits for **every** run that set its own | DECISION-9672c566 |
| 2 | Fable 5.1 | M1: the label covered the briefing's inputs only, so a child that fetched a page **through its own tool** handed the text to its parent labelled clean, with its own history showing the tool result untrusted and tainted | DECISION-302fc6ff |

Read both decisions. Assume a third defect of the same shape is present: something a run records
about itself that is not what happened. You are a different model from round 2; do not inherit
its coverage or round 1's.

The requirements are FR-70, FR-71, FR-72, AC-56, AC-57 and NFR-22, quoted below. Decisions:
DECISION-35f4c3f4 (the pre-flight's four clarifications), P2-D21, DECISION-21e5d2e9 (M17's K1 and
K3 fixed here), and the two above. Pre-flight: KNOWLEDGE-1f2f0e44. D13 applies.

**M18 is not committed.** HEAD is `4cfb054` (M17). Review it with:

```bash
git diff 4cfb054 -- agentsdk tests scripts README.md
git status --short
```

### What changed since round 2

- **M1.** A child's inputs are its briefing **and whatever it read while running**. The pool now
  reads the child's own session history after the run, through a new underscored
  `Runner._history_for`, and folds the provenance of every tool result into the answer's label.
  Tested on both stores with a tool declaring external provenance, as the shipped fetch and search
  tools do, and with a clean tool to show the label does not simply taint everything.
- **A judgement to check:** a pool that **cannot read** the child's history now **fails the node**
  rather than handing text to a parent with a label it cannot justify. That turns a store failure
  into a failed node, which cuts against "accounting never fails a run" (NFR-11). The author chose
  the security property over the availability one because FR-70 is about what crosses a boundary.
  Say if you disagree.
- **M2.** An input's provenance is read before its bytes, so nothing is taken in before it can be
  accounted for. Both endings are pinned: a failure before anything is read stays clean, and a
  known provenance survives a failed read.
- **Tests:** 5 added (31 to 36), each seen failing first; mutants M1a, M1b, M1c and M2a added, and
  S4 retargeted at the inputs half of the label.

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

- `agentsdk/subagents.py`: the pool, the briefing, the result, `_what_it_read`, and both fixes.
- `agentsdk/api.py`: `_history_for` (new), `_limits_for` (round 1's L2b), `RunConfig.depth` and
  `RunConfig.output_schema`.
- `agentsdk/scheduler.py`, `agentsdk/loop.py`, `agentsdk/context.py`,
  `agentsdk/providers/openai_compatible.py`.
- `agentsdk/budget.py`, `agentsdk/handle.py`: M17's K3 and K1.
- `agentsdk/__init__.py`, `README.md`, `scripts/16_subagents.py`, `tests/test_distribution.py`,
  `tests/test_concurrency.py`, `tests/test_budget.py`; `tests/test_subagents.py` (36 tests).

## What the author ran for round 3

1. **Reproduced M1 first** on both stores, then wrote the tests, which failed on the round 2 code.
2. **Gate file:** 36 passed. **Full suite:** 1504 passed in 254.29 s.
3. **Demo command:** 7 of 7 offline and live.
4. **Mutation run**, files restored and SHA-256 verified: **36 of 37 killed**, D11 declared.
5. **Gates.** Both green on source hash `c4fe9507`, the hash KICKOFF.md records, from one
   `genesis gate` run.
   - `unit`: `.venv\Scripts\python.exe -m pytest tests/test_subagents.py -q`, exit 0, 36 passed in 3.95s (10:47:11 to 10:47:16 UTC).
   - `regression`: `.venv\Scripts\python.exe -m pytest -q`, exit 0, 1504 passed in 254.29s (0:04:14)
     (10:47:16 to 10:51:32 UTC).

## Attack these first

**Report a result for every item, `probed: <what you ran and what it showed>` or `not probed:
<why>`.** Save probe scripts under

`C:\Users\<user>\AppData\Local\Temp\claude\<session-folder>\7dae5546-45c4-45ef-a67e-cb3e638507df\scratchpad\m18r3-probes\`

with shared cleanup in one `common.py`, and tenant ids starting `SYN-m18r3`. The earlier rounds'
probes are in `m18r1-probes\` and `m18r2-probes\`.

1. **Can anything still cross with a label cleaner than its source?** Tools of every provenance,
   several tools in one run, a tool that fails, an MCP-bridged tool, a child that reads an
   artifact through a tool rather than a briefing, a nested child (does the grandchild's taint
   reach the grandparent?), a re-asked structured child, a cancelled child.
2. **The new read of the child's history.** Is it the right history on both stores, for the right
   run, with nothing of another run in it? What happens when the session store is slow, empty, or
   holds messages from a resumed run? Is the fail-the-node judgement above right?
3. **What every run records about itself**, children and non-children alike: limits, pricing,
   budget, parent, scope, principal context, manifests.
4. **The structured contract** end to end with a real provider and a stubbed one.
5. **The limits** under real parallelism, per parent, nested trees (see M5 below).
6. **NFR-22 and AC-44** after a fan-out, and nothing left running.
7. **The demo command**, checked against the stores rather than its printout.

## Declared limitations: known, recorded, NOT findings

- **D11 survives:** `RunConfig.depth` is written by the pool and read by nothing until M19. Round 1
  verified this and verified that recording it in the `RunStarted` payload breaks an approved M14
  test.
- **Round 2's caveats, carried:** M3, the manifest cannot tell a structured child from a plain one;
  **M4**, a pool's limits replace the Runner's per-tool limits for children, so a child may run
  more tools at once than the Runner would allow -- recorded consistently, widened silently;
  **M5**, limits are counted per parent scope, so a nested tree can reach N + N² children at once;
  M6, an invalid JSON Schema is reported as the model's failure; M8, `SubagentResult.artifacts` is
  always empty until tools get a run scope (FR-76, M19).
- **Round 1's caveats:** the adapter's refusal check is a substring match; depth and parent are
  caller-trusted with no ancestry check (D13); `principal_context` is passed, not inherited.
- **A weak assertion, recorded:** round 1 changed an approved M11 test to compare the stored limits
  against `to_json()` itself, so a key dropped from `to_json` would vanish from both sides. Worth
  your eye.
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

