You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**Use a fresh model: this round is for Sonnet 5.** M16 was implemented by Opus 5 and its round 1
was reviewed by Fable 5.1; M15 was reviewed by Sonnet 5 and M14 by Opus 5 and Fable 5.1. If you
have reviewed M16 before, say so and ask for a different session. Across M5 to M16 nearly every
defect was found in a region the previous reviewer had not examined, and round 1's F3 was in a
place its own brief named. Re-derive the requirements from SPEC.md rather than inheriting the
framing below.

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

**M16-plan-persistence**, **round 2**: the plan is a persisted immutable object: PlanNode and
PlanVersion with parent_plan, schema-validated planner output, and a RunStateStore holding node
status on both stores.

Round 1 (Fable 5.1, 2026-09-19T14:23:45Z) approved with caveats F1 to F5. The owner verified F1 to
F3 first-hand and argued that F3 breaks AC-51; the implementing session agreed and decided that F3
blocks completion, fixing F1 and F5 in the same pass (**DECISION-6d073ac0**, read it first). That
approval is now stale because the code changed. Your job is to judge the fixed code, and M16 as a
whole, not just the diff: round 1's reviewer is a different model and you should not inherit its
coverage.

The requirements are FR-64, FR-65, FR-66, AC-51, AC-52 and NFR-22, quoted below. Other decisions
that bear on it: P2-D15 to P2-D18 (DECISION-8e6927c4), P2-D27 (DECISION-c49af041), and
DECISION-8f8cc54c, the owner's two pre-flight clarifications (a transition writes its status row
and emits through the run's own sink with the node id in `task_id`; M16 stores the shapes of
criteria and reservations with no behaviour). The pre-flight is KNOWLEDGE-9a3abf61. D13 applies
(DECISION-2bad84bb): in-process caller code is trusted.

**Nothing of M16 is committed**, and neither is the increment 2 specification it builds on. HEAD
is `b3654cf` (M15). No snapshot of the round 1 tree was kept, so there is no round 2 patch; the
changes are listed by place below. Review the whole milestone with:

```bash
git diff b3654cf -- agentsdk tests scripts pyproject.toml README.md
git status --short   # new: agentsdk/plan.py, agentsdk/schemas/, agentsdk/migrations/0007_plans.sql,
                     #      scripts/14_plan.py, tests/test_plan.py
```

### What changed since round 1

- **F3, the blocking one.** On Postgres, JSONB rewrote numbers inside `expected_output_schema` and
  criterion `arguments` (1e16 read back as the integer 10000000000000000, -0.0 as 0), so a plan
  read back with a different `plan_hash`, silently and from valid input. Now:
  - `plan.canonical_text()` is the one function for the text a plan is hashed over and the text
    Postgres stores; `plan_versions.document` is `TEXT`, not `JSONB`.
  - `PostgresRunStateStore._select_versions` rebuilds each plan from that text and refuses a row
    whose recomputed hash differs from its stored `plan_hash`, or that is no longer a valid plan,
    with a new public `PlanIntegrityError` (an `AgentSDKError`, beside `ArtifactIntegrityError`).
    This closes F3b: a document edited in the database used to read back without error.
- **F1.** `plan_id` was a global key, so another tenant could store a version under an existing
  plan id, blocking the owner's replan and learning from "already stored" that the plan existed.
  Now a plan is identified by tenant, project, plan_id and version on both stores:
  - both tables' primary keys lead with `tenant_id, project_id`, and the parent foreign key stays
    inside the scope;
  - every version of one plan belongs to one run (`plan ... belongs to run ..., not run ...`), and
    on Postgres a transaction-scoped advisory lock on (tenant, project, plan_id) stops two runs
    claiming a new plan id at once;
  - the in-memory store keys every row by the same four values (`_key`), and `put_plan` checks the
    named parent first, then ownership, then duplication.
- **F5.** Construction raised `RecursionError` for JSON nested 200 or more levels, breaking the
  pinned contract that every refusal is `InvalidPlan`. `_json_object` now refuses anything nested
  more than `MAX_JSON_DEPTH = 64` levels, with an iterative walk (`_too_deep`) that cannot itself
  recurse too deep.
- **Migration 0007 was rewritten in place**, not superseded: it was never committed or released, and
  the owner chose this over a new migration that would renumber the specification. On the
  development database its two tables, then holding only 14 `example-tenant` demo rows, were
  dropped and its `schema_migrations` row removed, in one transaction guarded by an exact count of
  those rows; the next session applied the new 0007. **Check that this left nothing inconsistent.**
- **Tests:** 28 added to `tests/test_plan.py` (114 to 142): nine JSON value shapes on both stores,
  a tampered document refused on read, depth 200 and 800 refused as `InvalidPlan` directly and
  through a planner's document, depth 50 still accepted, scoped plan ids on both stores, one run
  per plan id on both stores, and two runs racing to claim a new plan id. The migration test now
  pins `document` as `text` and both primary keys. One new test was vacuous in memory when first
  written, because two separately built in-memory stores share nothing; it now uses `for_scope`,
  and was seen failing on both stores before the fix.

### Carried to M19, NOT for this round (DECISION-6d073ac0)

- **F2:** a transition accepts any event sink, so a node event can land in another run.
- **F4:** on Postgres the row lock ends at commit before the emit, so two racing transitions of one
  node can record Finished before Started.

Report either only if the fixes above made it worse.

### Requirements it claims to satisfy (verbatim from SPEC.md)

- **FR-64:** `PlanNode` is a public frozen type with `node_id` (an exact non-empty `str`, unique within its plan), `objective`, `dependencies` (node ids in the same plan), `assigned_role`, `input_refs`, `expected_output_schema` (a JSON Schema document or None), `acceptance_criteria` (FR-74), `budget_reservation` (FR-67) and `side_effecting`, a bool defaulting to True (P2-D18), with optional `timeout`, `retry_policy` and `risk_class`. Every field is validated at construction by field name, as `SchedulerLimits` is (FR-43): a value the store cannot hold is refused there (`unstorable_reason`), so no plan completes in memory and fails on Postgres. A plan whose dependencies name an unknown node, or form a cycle, is refused: ADR-02 is DAG-only, and P2-D17 adds no loop node.
- **FR-65:** `PlanVersion` is immutable and carries `plan_id`, `version`, `parent_plan` (the `plan_id` and `version` it was replanned from, or None for the first), `run_id`, `nodes`, `created_at` and `plan_hash`, the hash of its canonical JSON. A planner's output is a JSON document validated against a schema shipped with the package; a document that fails validation is refused with the failing path, and no partial plan is stored. Storing a plan never mutates an earlier version: a replan writes a new version whose `parent_plan` points at the old one, so the chain of what was tried is readable after the run. Migration `0007` adds `plan_versions` and `plan_node_states`, both carrying non-null indexed `tenant_id` and `project_id` (NFR-2), idempotent per FR-17.
- **FR-66:** `RunStateStore` records each node's status for a run: `pending`, `ready`, `running`, `done`, `failed`, `skipped`, and `cancelled`. Transitions are recorded through the same event stream as everything else, so a consumer following `RunHandle.events()` (FR-49) sees `PlanNodeStarted` and `PlanNodeFinished` envelopes carrying `node_id`, `plan_id` and `version`, with `sequence_no` unique and contiguous (NFR-17). Both stores implement it, and a node's status is read back exactly as written. `scripts/14_plan.py` builds a two-node plan, stores it, replans it once and prints both versions with their parent link.
- **NFR-22:** Orchestration never corrupts the record. Under parallel subagents, replanning and cancellation, every run's `sequence_no` values stay unique and contiguous on both stores, no child is left `running` once its parent has returned or raised, every child's `parent_run_id` resolves to a run that exists, and a plan version is never mutated after it is stored (NFR-17).
- **AC-51:** On both stores, a plan round-trips: a two-node `PlanVersion` is stored, read back field for field with its `plan_hash` unchanged, replanned once, and the new version's `parent_plan` resolves to the first, which is itself unchanged on disk. A plan whose dependencies name an unknown node, and one whose dependencies form a cycle, are each refused at construction naming the node.
- **AC-52:** Applying migration `0007` to a database at `0006` that holds runs creates `plan_versions` and `plan_node_states` with non-null `tenant_id` and `project_id`, and applying it twice changes nothing (FR-17).

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

- `agentsdk/plan.py`: the types, `plan_from_document`, the DAG check, `canonical_text`, the depth
  bound, the transition rules shared by both stores, and `InMemoryRunStateStore`.
- `agentsdk/schemas/plan.schema.json`, the planner's document.
- `agentsdk/migrations/0007_plans.sql`, rewritten (above).
- `agentsdk/postgres.py`: `PostgresRunStateStore`, appended; its imports.
- `agentsdk/persistence.py`: `Persistence.run_state_store`.
- `agentsdk/errors.py`: `InvalidPlan`, `PlanNotFound`, `PlanIntegrityError`.
- `agentsdk/events.py`: `PLAN_NODE_STARTED` and `PLAN_NODE_FINISHED`.
- `agentsdk/__init__.py`, `pyproject.toml` (package data), `README.md`.
- `scripts/14_plan.py`, the demo command; `tests/test_distribution.py` lists it.
- `tests/test_plan.py` (142 tests); `tests/conftest.py`: the two tables in `_TRACKED`.

## What the author ran for round 2

1. **Tests first.** With the new tests and the round 1 code: 12 failed, each fix with a failing test
   on the store it affects. The memory half of the scoped-id test passed vacuously at first; after
   switching it to `for_scope` it failed on both stores.
2. **The round 1 reviewer's own probes**, rerun against the fixed code from
   `m16r1-probes\`: `p02_hash.py` now reports `postgres failures: []` for all its value shapes, and
   its tamper step, which asserted that a tampered document "reads back silently", now raises
   `PlanIntegrityError`. `p04_document.py` refuses every depth from 200 up as `InvalidPlan` and
   still accepts 50. `p01_scope.py`'s remaining `[FAIL]` lines assert that tenant B storing under
   A's plan id is *refused*; under scoped identity it is B's own plan and is accepted, while every
   check of A's side passes. Its F2 line is unchanged, as recorded.
3. **Mutation run**, every file restored and SHA-256 verified, every `.py`, `.sql` and `.json` under
   `agentsdk/` hashed before and after: **41 of 41 killed**, each by the test written for it. New
   for round 2: the in-memory key losing its scope, a plan taking a second run (both stores), the
   depth check removed and its bound lifted, the stored hash never compared, the advisory lock
   removed (killed on 4 of 4 runs), and 0007 going back to JSONB or to an unscoped key. Mutants of
   0007 run the migration test alone with `--noconftest`, because the live database's checksum
   guard would otherwise kill them for the wrong reason (round 1's A3).
4. **Demo command, both modes:** 8 of 8 checks each, with the same plan hashes as before the fix.
5. **Residue:** no `SYN-m16` rows and no throwaway schemas after the author's reruns.
6. **Gates.** Both green on source hash `8f5e56d9`, the hash KICKOFF.md records, from one
   `genesis gate` run; `independent-review` reads stale, as it should until round 2 records.
   - `unit`: `.venv\Scripts\python.exe -m pytest tests/test_plan.py -q`, exit 0, 142 passed in 16.47s
     (15:08:20 to 15:08:37 UTC).
   - `regression`: `.venv\Scripts\python.exe -m pytest -q`, exit 0, 1375 passed in 254.54s (0:04:14)
     (15:08:37 to 15:12:53 UTC).

## Attack these first

**Report a result for every item, `probed: <what you ran and what it showed>` or `not probed:
<why>`.** Save every probe script under

`C:\Users\<user>\AppData\Local\Temp\claude\<session-folder>\7dae5546-45c4-45ef-a67e-cb3e638507df\scratchpad\m16r2-probes\`

with shared redaction and cleanup in one `common.py`, and use tenant ids starting `SYN-m16r2`.
Round 1's probes are in `m16r1-probes\` beside it; reuse them, but judge their expectations
yourself (item 2 of the list above explains why some of `p01_scope.py` now reports FAIL).

1. **F3 end to end.** Does any value now change between build, store and read, on either store:
   floats of every magnitude, negative zero, integers beyond 2**63, unicode, escapes, and anything
   else JSON can hold? Does the integrity check fire for every way a row can be edited, including
   the `plan_hash` column itself, and never for an honest row?
2. **F1 end to end.** Can another tenant, another project or another run of the same scope block,
   read, move or detect a plan in any way, on either store: through `put_plan`, `get_plan`,
   `versions`, `node_states` or `transition`? Does the advisory lock hold under real concurrency,
   and can it deadlock or starve anything?
3. **F5.** Is there any path left where construction or a read raises something other than
   `InvalidPlan`, `PlanNotFound` or `PlanIntegrityError`?
4. **The rewritten 0007 and the reset.** Shape, keys, constraints and idempotency; applying it to a
   database at 0006 that holds runs; and whether the development database's recorded checksum,
   tables and rows are now consistent with the file on disk.
5. **Everything round 1 covered, again**, on the code as it now is: scope, immutability and the
   hash, the DAG check, the planner's document, transitions and their events, NFR-22 and AC-44,
   and the demo command checked against the stores rather than its printout.

## Declared limitations: known, recorded, NOT findings

- **F2 and F4**, carried to M19 (above).
- **A7:** a status row can exist without its event if the emit fails after the commit; round 1
  judged it not a breach of NFR-22 as written.
- **No behaviour for criteria or reservations**, and nothing drives nodes yet (DECISION-8f8cc54c).
- **The in-memory store is a test and offline double:** it holds everything in one process and
  scans its dict to find a plan's owner.
- **Round 1's A8 to A12** still stand as recorded in `M16-review-prompt.md`.
- **Older `independent-review` gates** compute stale because the repository hash moved.

## Rules

- **Do not fix the code.** Report defects; the implementing session repairs them.
- **Gates are computed, never narrated.** Paste real command output for anything you assert.
- **Approve if it is sound.** A defect must be reachable and must matter. Latent, out-of-scope or
  cosmetic findings are caveats in your reason, not blockers.
- **Restore every file you mutate** and verify SHA-256, restoring in a `finally`; kill the whole
  process tree on a timeout (`taskkill /T`). A mutant that edits `0007` must run the migration test
  alone with `--noconftest`, or the live checksum guard kills it for the wrong reason (A3).
- **Clean up** every run, row, temp folder and process you create, and say what you removed.
- **Report every probe**, including ones that showed nothing.

## Recording your decision

Use **single quotes** around `--reason`, and keep apostrophes, backticks and `$` out of it.

```bash
# pass
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control approve . M16-plan-persistence --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M16-plan-persistence --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, the result for each of the five attack items, and
your verdict.

