You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**Use a fresh model.** Across M5 to M15, nearly every defect was found in a region the previous
reviewer had not examined. If you have reviewed this project before, say so and ask for a
different session. M16 was implemented by Opus 5. M15 was reviewed by Sonnet 5, and M14 by Opus 5
(round 1) and Fable 5.1 (round 2). Re-derive the requirements from SPEC.md rather than
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

**M16-plan-persistence**: the plan is a persisted immutable object: PlanNode and PlanVersion with
parent_plan, schema-validated planner output, and a RunStateStore holding node status on both
stores.

First review round, and the first milestone of Phase 2 increment 2. The requirements are FR-64,
FR-65, FR-66, AC-51, AC-52 and NFR-22, quoted below. Decisions that bear on it: P2-D15 to P2-D18
(DECISION-8e6927c4), P2-D27 (DECISION-c49af041), and **DECISION-8f8cc54c**, the owner's two
clarifications made at M16's pre-flight: a node transition writes its status row and emits its
event through that run's own sink, with the node id in the envelope's `task_id`; and M16 fixes the
stored shapes of acceptance criteria and budget reservations with no behaviour, which M19 and M17
add. The pre-flight is KNOWLEDGE-9a3abf61. D13 applies (DECISION-2bad84bb): in-process caller code
is trusted.

**Nothing of M16 is committed**, and neither is the increment 2 specification it builds on. HEAD
is `b3654cf` (M15). Review it with:

```bash
git diff b3654cf -- agentsdk tests scripts pyproject.toml README.md
git status --short   # new: agentsdk/plan.py, agentsdk/schemas/, agentsdk/migrations/0007_plans.sql,
                     #      scripts/14_plan.py, tests/test_plan.py
```

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

- `agentsdk/plan.py` (new): `PlanNode`, `AcceptanceCriterion`, `BudgetReservation`,
  `NodeRetryPolicy`, `PlanVersion`, `plan_from_document`, the DAG check, the canonical hash, the
  transition rules shared by both stores, and `InMemoryRunStateStore`.
- `agentsdk/schemas/plan.schema.json` (new): the planner's document, Draft 2020-12, shipped as
  package data.
- `agentsdk/migrations/0007_plans.sql` (new): `plan_versions` and `plan_node_states`.
- `agentsdk/postgres.py`: `PostgresRunStateStore`, appended; three imports.
- `agentsdk/persistence.py`: `Persistence.run_state_store`.
- `agentsdk/errors.py`: `InvalidPlan` (an `AgentSDKError` and a `ValueError`, with `path`) and
  `PlanNotFound`.
- `agentsdk/events.py`: `PLAN_NODE_STARTED` and `PLAN_NODE_FINISHED`.
- `agentsdk/__init__.py`: the new public names.
- `pyproject.toml`: `schemas/*.json` in package data.
- `scripts/14_plan.py` (new), the demo command; `tests/test_distribution.py` lists it.
- `tests/test_plan.py` (new, 114 tests); `tests/conftest.py`: the two tables in `_TRACKED`.
- `README.md`: the test count and the example row.

## What the author ran

1. **Pre-flight**, recorded as KNOWLEDGE-9a3abf61: a baseline of 1233 passed in 250.23 s at
   `b3654cf`, instrumented; the code facts M16 extends; and the two spec gaps the owner then
   decided as DECISION-8f8cc54c.
2. **Tests first.** Against no implementation: 96 failed and 1 passed. The failures were 79
   `ModuleNotFoundError` for `agentsdk.plan`, 14 `AttributeError` for
   `Persistence.run_state_store`, and 3 missing pieces: migration 0007, the example, the public
   names. The pass was the session-guard check against the conftest edit.
3. **After the implementation:** 97 passed; the full suite 1330 passed in 247.36 s.
4. **Demo command, both modes:** `python scripts/14_plan.py --offline` and
   `python scripts/14_plan.py` (live: gateway and Postgres), 8 of 8 checks each. The two modes
   print identical plan hashes (`6d28cd9e...` and `cfe2b80c...`), so a plan read back from JSONB
   hashes the same as the one written.
5. **Mutation run**, every file restored and SHA-256 verified, and every `.py`, `.sql` and `.json`
   under `agentsdk/` hashed before and after: final result **33 of 33 killed**, each by the test
   written for it. How it got there is A3 to A6 below.
6. **Gates.** Both green on source hash `65686fe0`, the hash KICKOFF.md records, from one
   `genesis gate` run.
   - `unit`: `.venv\Scripts\python.exe -m pytest tests/test_plan.py -q`, exit 0, 114 passed in 13.54s
     (13:35:09 to 13:35:23 UTC), evidence `.genesis/evidence/M16-plan-persistence-unit.json`.
   - `regression`: `.venv\Scripts\python.exe -m pytest -q`, exit 0, 1347 passed in 249.26s (0:04:09)
     (13:35:24 to 13:39:34 UTC), evidence `.genesis/evidence/M16-plan-persistence-regression.json`.
   - An earlier gate run, before A6's fix, was green too, on `d0d971de` with 102 and 1335.

## Issues the author found during M16

| id | finding | disposition |
|---|---|---|
| A1 | The gate's parametrised node-field test passed `node_id` twice, once through the helper's positional argument | fixed in the test before any implementation claim |
| A2 | The example test called the script with no argument; every example runs offline through `--offline` (AC-17) | test aligned with the convention; the assertions did not change |
| A3 | The first mutation run reported 29 of 33 killed, three of them falsely. G1, G3 and G4 used an untyped `%s IS NOT NULL`, which Postgres cannot type, so the query errored in the first Postgres test. MG1 and MG2 edited 0007, whose checksum the live database had recorded, so the session setup refused it before the migration test ran | the SQL mutants were retyped; the migration mutants run the migration test alone with `--noconftest`, where it passes unmutated. G1 and G3 were then killed by their own tests, and **G4 survived** |
| A4 | Survivors: D6 (a document's DAG error carried no path), I3 and G4 (a replan's parent could belong to another run, although FR-75 keeps a replan inside its run), and G5 (without the row lock, racing transitions could finish one node twice and emit two `PlanNodeFinished`) | three tests added; all killed, G5 on 4 of 4 runs, and the race test passed 5 of 5 against the real code |
| A5 | D3, not copying the planner's document, survived because every node already copies and freezes what it holds | declared equivalent, and the redundant copy removed |
| A6 | Found by probing, not by mutation: a USD reservation such as `Decimal("1E+2")` or `Decimal("1E-7")` was written with `str()`, which gives exponent form, and the document's price pattern refuses that. Such a plan built and stored, and on Postgres `get_plan` would then refuse its own row | fixed with fixed-point formatting; a test over six values on both stores failed first, 6 of 12, and passes now; mutant P11 restores `str()` and is killed |
| A7 | A transition commits the status row and then emits the event through the run's sink, which on Postgres uses its own connection. If the emit fails after the commit, the row exists without its event | not fixed; declared below |
| A8 | Offline, the example sends node events to a fresh in-memory sink for the finished run, numbered from 1, because the run's own in-memory sink ended with it. Live, they join the run's stored events and continue its numbering | the example says so in a comment |
| A9 | `agentsdk.postgres.RunScope` already exists as a store-level scope, and FR-76 introduces a `RunScope` for tools in M19 | out of scope; flagged for M19's pre-flight |
| A10 | `PlanNode.retry_policy` had no defined shape; the only `RetryPolicy` in the code is the HTTP client's | the author chose `NodeRetryPolicy(max_attempts)`, under the owner's shapes-now decision |
| A11 | `plan_hash` covers the plan's document only, not `plan_id`, `version`, `run_id` or `created_at`, so it names what the plan says. A replan with identical nodes hashes the same | a design choice, pinned by a test |
| A12 | The live demo writes rows to the development database under tenant `example-tenant`, as examples 04 and 12 do | expected; not test residue |

## Attack these first

**Report a result for every item, `probed: <what you ran and what it showed>` or `not probed:
<why>`.** At M13 round 1 a reviewer approved without probing the top item, and the owner had to
check it by hand. Save every probe script under

`C:\Users\<user>\AppData\Local\Temp\claude\<session-folder>\7dae5546-45c4-45ef-a67e-cb3e638507df\scratchpad\m16r1-probes\`

with shared redaction and cleanup in one `common.py`, and use tenant ids starting `SYN-m16r1`, so
the implementing session can rerun each probe on its own.

1. **Tenant and project scope, both stores.** Every read, write and transition, including
   `versions`, `node_states`, a transition through another scope's store, a plan naming another
   scope's run, and `for_scope` in memory.
2. **The plan never changes, and its hash never lies.** Any path that yields a stored plan that
   reads back different, or a `PlanVersion` whose `plan_hash` does not match its own
   `to_document()`: deep mutation through every mapping and tuple, Decimal forms (A6 was one),
   floats, very large integers, unicode and JSONB's own normalisation of numbers and keys.
3. **The DAG check.** Cycles of every length, self-loops, unknown dependencies, duplicate ids, and
   large or deep plans; the check is iterative on purpose.
4. **The planner's document.** Schema against construction: something the schema admits but the
   type refuses, or the reverse; a document that validates but cannot be stored; the `path` on
   every refusal.
5. **Transitions and their events.** Final states, the ban on returning to pending, racing
   transitions on Postgres, `task_id` and the payload, and `sequence_no` contiguity alongside the
   run's other events. Judge A7 yourself: is a status row without its event a breach of NFR-22?
6. **Migration 0007.** Shape, constraints, idempotency, and applying it to a database at 0006 that
   holds runs.
7. **NFR-22 and AC-44.** No residue after a full run; every child row references a run that
   exists.
8. **The demo command.** Run `python scripts/14_plan.py` both ways and check its claims against the
   stores, not its own printout.

## Declared limitations: known, recorded, NOT findings

- **A7:** a status row can exist without its event if the emit fails after the commit.
- **No behaviour for criteria or reservations.** M19 evaluates criteria and M17 enforces
  reservations (DECISION-8f8cc54c); M16 stores their shapes.
- **Nothing drives nodes yet.** Seeing node events live through `RunHandle.events()` is proven
  by M19's AC-58, per DECISION-8f8cc54c.
- **A8, A9, A10, A11, A12** above.
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

Then report: what you checked, what you ran, the result for each of the eight attack items, and
your verdict.
