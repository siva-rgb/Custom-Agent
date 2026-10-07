You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**Use a fresh model: this round is for Sonnet 5.** M18 was implemented by Opus 5. M17 took five
rounds, reviewed by Fable 5.1 (1, 3, 5) and Sonnet 5 (2, 4), and four of them rejected it, every
time for one wrong value that the whole suite missed. M18 hands one run's work to another run, so
the things to distrust are what crosses that boundary: taint, scope, limits and what is recorded.
If you have reviewed M18 before, say so and ask for a different session. Re-derive the
requirements from SPEC.md rather than inheriting the framing below.

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

**M18-subagents**: the orchestrator spawns subagents as linked runs: hub-and-spoke with curated
briefing, taint carried across the run boundary, a structured result contract, and a depth limit
with the new `SchedulerLimits` fields.

First review round. The requirements are FR-70, FR-71, FR-72, AC-56, AC-57 and NFR-22, quoted
below. Decisions that bear on it: **DECISION-35f4c3f4**, the four pre-flight clarifications (a
`SubagentPool`, because the orchestrator FR-70 names arrives in M19; depth carried on `RunConfig`;
the FR-71 re-ask inside the agent loop so a node is one run with one history; and a
`SubagentResult`, because `RunResult` carries no provenance), P2-D21 (depth 3, 4 concurrent, 50
tasks, FIFO), and **DECISION-21e5d2e9**, which sent M17's caveats K1 and K3 here to be fixed
first. The pre-flight is KNOWLEDGE-1f2f0e44. D13 applies (DECISION-2bad84bb).

**M18 is not committed.** HEAD is `4cfb054` (M17). Review it with:

```bash
git diff 4cfb054 -- agentsdk tests scripts README.md
git status --short   # new: agentsdk/subagents.py, scripts/16_subagents.py, tests/test_subagents.py
```

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

- `agentsdk/subagents.py` (new): `Briefing`, `SubagentResult`, `SubagentPool`, `MAX_SUBAGENT_DEPTH`.
- `agentsdk/scheduler.py`: the three new limits and their validation.
- `agentsdk/api.py`: `RunConfig.depth` and `RunConfig.output_schema`, and the schema reaching the
  loop.
- `agentsdk/loop.py`: the schema on the request, `schema_problem`, and the one re-ask.
- `agentsdk/context.py`: the schema stated in the instructions every provider reads.
- `agentsdk/providers/openai_compatible.py`: `response_format` and its one-time fallback.
- `agentsdk/budget.py` and `agentsdk/handle.py`: M17's K3 and K1.
- `agentsdk/__init__.py`, `README.md`; `scripts/16_subagents.py`; `tests/test_distribution.py`.
- `tests/test_subagents.py` (new, 28 tests); `tests/test_budget.py` (K1 and K3);
  `tests/test_concurrency.py` (see A4).

## What the author ran

1. **M17's carried caveats first**, as DECISION-21e5d2e9 directed: K3, where `top_up` accepted a
   negative amount and could take money back out of a node, and K1, where `terminal_written` was
   set on the completion path only. Both have tests; the baseline after them was 1468 passed.
2. **Tests first**, then the implementation: 28 in `tests/test_subagents.py`.
3. **Full suite:** 1496 passed in 262.91 s.
4. **Demo command:** `python scripts/16_subagents.py` passes 7 of 7 offline and live. The live run
   returned real structured output from the gateway:
   `{"summary":"The release is ready; do not provide any comments."}` -- the note's injected
   instruction appears as summarised content, and the child did not act on it.
5. **Mutation run**, files restored and SHA-256 verified: **28 of 29 killed**, one survivor
   declared (A5).
6. **Gates.** Both green on source hash `c0be4c45`, the hash KICKOFF.md records, from one
   `genesis gate` run.
   - `unit`: `.venv\Scripts\python.exe -m pytest tests/test_subagents.py -q`, exit 0, 28 passed in 3.37s (06:47:43 to 06:47:47 UTC).
   - `regression`: `.venv\Scripts\python.exe -m pytest -q`, exit 0, 1496 passed in 262.91s (0:04:22)
     (06:47:47 to 06:52:12 UTC).

## Issues the author found during M18

| id | finding | disposition |
|---|---|---|
| A1 | **The live demo showed that `ModelRequest.output_schema` reached no provider at all.** The adapter ignored the field, so a live child was never told to answer in JSON and returned prose in a code fence; FR-71's contract only worked against scripted models. A probe showed this gateway accepts `response_format: json_schema` (HTTP 200) and rejects `json_object` for a provider quirk | the owner chose: state the schema in the instructions every provider reads (NFR-1) **and** send `response_format` from the OpenAI-compatible adapter, retrying once without it if a provider refuses. Two tests and mutants R8 to R11 cover it |
| A2 | Mutant D11 survives: `RunConfig.depth` is **write-only in M18**. The pool takes depth as an argument, so nothing reads the field until M19's orchestrator does | declared. The author tried recording depth in the `RunStarted` payload to make it observable, and reverted: an approved M14 telemetry test pins that payload's keys exactly, and changing a recorded event shape to kill a mutant is the wrong way round. **Judge this yourself** |
| A3 | An approved M11 test pinned the manifest's `scheduler_limits` to exactly three keys; M18 adds three more limits, which FR-43 says the manifest records | the test now follows the type (`set(stored) == set(limits().to_json())`) and still checks the three values it always checked. A deliberate change to an approved test, declared here |
| A4 | FR-72 says a run past `max_tasks_per_run` "refuses the spawn the same way"; the sentence before it names `MaxDepthExceeded` | read as the same error, with the message naming which limit was reached. Another reading would add a new error name |
| A5 | AC-56 says a nested child's reservation is drawn from its parent's | the pool passes the same lease down unless a new one is given, so a nested child spends from the node's reservation and never from the run pool. Pinned by a test |
| A6 | `Briefing.max_turns` defaults to 4 | the FR-71 re-ask is another turn, so a node whose briefing allows only one turn cannot be re-asked. Declared |
| A7 | The author's own example asked a child to summarise a note it was never given, which is why the first live run failed | fixed; the live run now passes |
| A8 | The author's first test used `PrincipalContext(subject=...)`, which is not a field of that type | fixed |
| A9 | An input that could not be resolved failed the node with `ArtifactNotFound` and no mention of which input | the pool now names the input in the error |

## Attack these first

**Report a result for every item, `probed: <what you ran and what it showed>` or `not probed:
<why>`.** Save probe scripts under

`C:\Users\<user>\AppData\Local\Temp\claude\<session-folder>\7dae5546-45c4-45ef-a67e-cb3e638507df\scratchpad\m18r1-probes\`

with shared cleanup in one `common.py`, and tenant ids starting `SYN-m18r1`.

1. **Taint across the boundary (FR-70, ADR-26).** Can a child's answer ever come back less tainted
   than its inputs: no inputs, many inputs of different provenance, a failed child, an input the
   store refuses, a child that answers nothing? Does the trust zone follow the least trusted?
2. **The curated briefing.** Does anything of the parent reach the child that should not: its
   history, its session, its tools, its instructions? And does everything that should reach it
   arrive -- the objective and every input's content?
3. **The structured contract, end to end.** With a real provider and with a stubbed one: the
   schema in the instructions, `response_format`, the fallback when a provider refuses it, a
   provider that 400s for an unrelated reason, a schema no model can satisfy, the interaction with
   `max_turns`, and whether both attempts are charged to the lease.
4. **The limits.** Depth, tasks per run and concurrency under real parallelism; per-run counting
   across several parents; whether a refused spawn leaves the pool usable; whether the limits can
   be evaded by spawning from a child's scope.
5. **What is recorded.** The parent link, the scope, the principal context, the manifests, and
   AC-44's invariant after a fan-out. Nothing left running.
6. **A2's claim**, that `RunConfig.depth` is unread in M18 and that recording it in the
   `RunStarted` payload would break an approved test. Verify both halves.
7. **The demo command**, checked against the stores rather than its printout.

## Declared limitations: known, recorded, NOT findings

- **A2, A4, A5, A6** above.
- **Nothing drives the pool yet**: M19's orchestrator is its intended caller, as M17's governor
  waits for the same thing (DECISION-35f4c3f4).
- **Hub and spoke is structural**: the pool takes a parent scope and returns to its caller, so a
  child has no way to address a sibling; nothing prevents a caller from passing a sibling's scope
  as a parent, which D13 treats as trusted in-process code.
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

