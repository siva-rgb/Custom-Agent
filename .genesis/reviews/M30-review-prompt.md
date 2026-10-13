You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**Use a fresh session: this round is for Fable 5.1, falling back to Sonnet 5** if Fable 5.1
fails to start. M30 was implemented by Opus 5.5. Earlier reviews: Opus 5 approved M29, Fable 5.1
approved M28, Sonnet 5 approved M27 and reviewed this increment's spec draft. Bring no memory of
an earlier review into this one: if your session holds any, say so and ask for a fresh one.

Re-derive the requirements from SPEC.md rather than inheriting the framing below:
- "#### M30": FR-113 to FR-117;
- AC-86;
- NFR-30 to NFR-32, which the task lists;
- FR-112 and AC-85, the record M30 writes;
- FR-44, FR-50, NFR-15 and NFR-22, which the turn changes.

Read the decision table P4-D1 to P4-D15, and the owner decisions DECISION-e03027a0,
DECISION-af27465a (issue order, a stopped pause, sticky scope) and DECISION-d9ddaf0f (the nine
spec-level choices). Read KNOWLEDGE-7ebaabe3 (M29's review), whose findings M30's pre-flight
settles.

## Repository

`<repo>` is the folder that holds this file's repository; run everything from it. The branch is
`phase-4`.

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The PATH prefix is required: graphizer shells out to python3, and without the venv first on PATH
that hits the Microsoft Store alias and crashes Node.

- **Configuration:** both gates need DATABASE_URL from .env; the regression gate also needs
  BASE_URL and MODEL_API_KEY. **Never print credentials or a connection string.** Pass every DSN
  through `agentsdk.config.normalise_database_url`. A raw `postgresql+psycopg://` DSN makes
  psycopg echo it in an error.
- **Host:** Windows Developer Mode must be on (D7). Python 3.11.

**Running tests**
- **Only one test session at a time, and nothing else in parallel with one.** Every pytest session
  compares the store's ids at its start and end (AC-44), and the development database is shared.
- Check that no other python or node process is running first.
- Run your own probes with `-p no:cacheprovider`: Genesis hashes .pytest_cache.
- **The full suite takes about 9 minutes.**

**Gates (DECISION-066e317a).** Run `genesis gate . M30-approvals --timeout 1500000` in the
background, or detached, since it outlasts a 10-minute foreground call. Then check for a stray
python process. If a gate reads `stale` straight after, the first run churned .pytest_cache: run
the gate file once with the cache on, then gate again. Writing any file under the repository
while a gate runs can stale it too.

**Mutations:** restore a mutated file byte for byte and verify its SHA-256 in a `finally`. Beware
`git stash`: on this host it rewrites line endings (KNOWLEDGE-ae83f412). **Do not mutate an
applied migration file, `0014` and `0015` included:** their checksums are recorded in the
database.

**The FR-81 fixture is active in every test.** A store call made from a coroutine on the event
loop fails the test at teardown.

**Cleanup order:** interruption rows reference their runs. Delete `interruption_decisions`, then
`interruptions` and `standing_decisions`, then the run's other rows, by run id.

## Task under review

**M30-approvals**, **round 1**, risk **high**. HEAD is `62cc198` (M29). A call that requires
approval now waits for a recorded decision. This is the first code that writes M29's tables.

### Pre-flight: M29's review findings (KNOWLEDGE-7ebaabe3)

1. **Migration `0015`:**
   - `standing_decisions` gains `UNIQUE (standing_id, tenant_id, project_id)`;
   - the single-column `interruption_decisions.standing_id` reference is dropped and replaced by a
     composite one;
   - the Python guard, which a decision's standing decision must pass, is now tested on both
     stores.
2. **`InMemoryInterruptionStore.interruptions`** returns nothing for an unreadable `run_id`, as
   PostgreSQL does.
3. **Lower priority:**
   - refusing every kind but `tool_approval` is tested in both stores;
   - the critic join ignores rows with no `critic_run`;
   - AC-85's no-update assertion now reads the stores' source;
   - the NFR-29 fixture's exclusion is narrowed to the four fields the record actually holds
     (`started_at`, `duration_ms`, `queued_ms`, `cost_usd`), in event payloads only.

### M30

- **The executor, `agentsdk/executor.py`:**
  - after step 5, `_approval` decides the trigger, in the order policy, then predicate, then hook,
    and builds the request, `describe` included. It refuses an unstorable request with
    `ToolApprovalCheckFailed`;
  - `prepare` returns `ApprovalRequired(interruption, prepared)`;
  - new methods: `rejected()` builds a rejection's result, labelled with `urn:agentsdk:decision:<id>`
    under internal trust, and `check_failed()` fails a call whose interruption could not be
    recorded.
- **The loop, `agentsdk/loop.py`:**
  - `_execute_tool_calls` is split into `_prepare_batch` and `_run_ready`. From the first batch
    with a call requiring approval, it prepares the rest of the turn, then calls `_approve`, then
    runs from that batch onward in issue order;
  - `_approve` registers the turn's interruptions as waited on **before** recording any;
  - `_approved` reads standing decisions once and records each interruption, together with a
    covering standing decision in one store step. It then waits on the store, re-reading on each
    wake, at least every 5 s, and at the earliest expiry;
  - a sticky decision made mid-wait decides waiting siblings;
  - expiry is decided by the run;
  - `_cancel_approvals` records a `cancel` decision on cancellation.
- **`agentsdk/interruptions.py`:**
  - `ApprovalWaits` is the in-process registry and wakes a run with `call_soon_threadsafe`;
  - `append_interruption(item, decision=None)` checks everything, then records both.
- **`agentsdk/api.py`:**
  - `RunConfig.approval_timeout`, which defaults to 3600 and must be finite and over 0, never a
    bool;
  - the manifest records it;
  - `Runner.decide` checks in this order: tenancy (`UnknownInterruption`), already decided,
    expired, not waiting. It records the decision, then the sticky row, then wakes the run;
  - `Runner.interruptions` reports the `waiting` flag.
- **`agentsdk/handle.py`:** `RunHandle.approve` and `reject` refuse an interruption whose top-level
  run is not the handle's run.
- **Elsewhere:**
  - `tools.py`: `Tool.needs_approval` and `Tool.describe`; `approval_policy` joins
    `schema_hash()` when it is not AUTO;
  - `errors.py`: `ToolApprovalRejected` and `ToolApprovalCheckFailed`, both exported;
  - `events.py`: `ApprovalRequested` and `ApprovalDecided`.
- **`scripts/24_approvals.py`** (FR-117): four turns, offline on both stores and live.
- **`tests/test_approvals.py`** (the unit gate): 49 tests.

### Edits to approved tests (each to judge)

- **`tests/test_builtin_tools.py`:** `test_a_tool_that_declares_nothing_keeps_its_pre_m10_schema_hash`
  (AC-32) is the one edit made to fit M30.
  - **The conflict:** its golden set includes specs declaring `REQUIRE_APPROVAL`, which hashed as
    AUTO ones while the policy was inert, and FR-113 puts the policy into the hash.
  - **The edit:** each golden spec is hashed with its policy at AUTO, which reproduces the pre-M10
    hashes exactly, and the declaring specs are asserted to hash differently now.
  - **Judge** whether that keeps AC-32's promise honest.
- **`tests/test_interruption_record.py`** (M29): the NFR-29 normalisation, narrowed as above.
- **`tests/test_distribution.py`:** `EXPECTED_EXAMPLES` gains `24_approvals.py`.
- **`tests/test_primitives.py`:** NFR-32 expected an edit to its "only Completed and Failed
  reachable" assertion. **None was needed:** `_results` still holds only those outcomes, because
  the loop settles every approval first.

## What changed

```bash
git diff 62cc198 -- agentsdk tests scripts README.md
git status --short
```

## What the author ran

1. **Tests first.** `tests/test_approvals.py` was written before the code. These test bugs were
   found on the way:
   - the test agent had no tool profile, so every call was denied before any approval (FR-113
     working);
   - a NUL in model arguments is refused at step 2 since Phase 0, so it cannot test an unstorable
     request; `describe` is used instead;
   - an ordering assumption was flaky: a turn's interruptions share `created_at`, so tests sort by
     call id.
2. **Two real races,** found when example 24 ran under the suite. Each was fixed, and each now has
   a deterministic test:
   - an interruption was listed pending before its run registered the wait, so an approver
     deciding at once was refused `InterruptionNotWaiting`;
   - a standing decision was applied only after the turn was recorded, so a covered call could be
     seen pending and decided by a person.
3. **Mutation run: 25 of 25 killed,** each file restored by SHA-256. Two first survived, both test
   gaps:
   - **"an unstorable request recorded":** the store refuses one too, so the two guards hid each
     other. The executor's guard now has its own test, with no store;
   - **"no re-read without a wake":** the dropped-wake test decided before the run first waited.
     It now waits for `ApprovalRequested`, and requires 0.5 s < elapsed < 6.5 s.
4. **Example 24 live** on the gateway and PostgreSQL: all 5 live checks passed, and the real model
   followed the four turns exactly. Its rows were removed by run id, interruptions first.
5. **Full suite before the gates:** one failure, the AC-32 conflict above.
6. **Gates**, both green on source hash `7634279a`, run in the background with `--timeout
   1500000`.
   - `unit`: 49 passed in 19.79s.
   - `regression`: 2109 passed in 515.03s (15:22:43 to 15:31:19 UTC). The baseline was 2060.

## Attack these first

**Report a result for every item, `probed: <what you ran and what it showed>` or `not probed:
<why>`.** Save probes in a folder `m30-r1-probes` in your own session's scratchpad, with tenant ids
starting `SYN-m30r1`; delete only rows your probes wrote, by run id, interruptions first.

1. **Nothing runs unapproved (NFR-30).** Find any path to step 6 for a call that requires
   approval without a recorded `approve`. Consider:
   - a store that fails at every point: recording, reading, standing, expiry;
   - cancellation at every await of `_approve` and `_approved`;
   - a decision racing the expiry;
   - a hook that changes the call after the request was built;
   - `concurrency_safe` batches mixing approval and free calls;
   - a tool whose predicate or `describe` has side effects or is slow.
2. **At most once and in issue order (P4-D13).** Two calls of one turn, one approved and one
   rejected, in every order. Duplicate call ids. Several turns. Do side effects ever reorder?
3. **Deciding (FR-115).**
   - Every refusal and its order, on both stores, tenancy first.
   - Can a decision be recorded for an interruption no run waits on, or after expiry? Use races,
     threads and other event loops.
   - Can a handle decide another run's call?
   - Are `decide`'s sticky and decision writes consistent when one fails?
4. **Sticky (P4-D15).** Policy-flagged only, siblings included, later turns included, exact
   decisions kept, sticky reject. Can a standing decision of another agent, tool or run apply?
5. **Waiting.**
   - The 5-second re-read, the earliest expiry, and lost wakes.
   - Cancellation: is every pending interruption decided `cancel`, and does the run end
     `cancelled` (P4-D14)?
   - What does a crash leave? Read `RunHandle` and `Runner.interruptions(waiting=)`.
6. **No change for runs needing no approval (NFR-32).** Requests and events unchanged; the
   NFR-29 fixture still passes. Is the hook's `REQUIRE_APPROVAL` the only deliberate change?
7. **Records and events.**
   - `ApprovalRequested` once per asked interruption, and none for one a standing decision
     covers.
   - `ApprovalDecided` before its call's `ToolCalled`.
   - Rejection text and label.
   - The manifest's `approval_timeout`.
   - `0015` applied and idempotent.
8. **The approved-test edits listed above,** especially AC-32's.
9. **Example 24 live,** if you can afford it, and **regressions:** the full suite, and the gate
   files of M26 to M29.

## Declared limitations: known, recorded, NOT findings

- **Everything declared for M20 to M29.**
- **Interrupted runs:** a paused run resumes only in the Runner that paused it. After a crash its
  rows stay undecided and `decide` refuses them (P4-D14); restart-surviving resume is Phase 6.
- **Orchestration is M32:** approvals inside a plan listed and decided from the top-level run, and
  the child's inherited timeout.
- **A plan's `tool_succeeds` criterion** cannot wait for approval: a tool needing approval there
  fails the criterion with `ApprovalRequired`.

## Rules

- **Do not fix the code.** Report defects; the implementing session repairs them.
- **Gates are computed, never narrated.** Paste real command output for anything you assert.
- **Approve if it is sound.** A defect must be reachable and must matter. Latent, out-of-scope or
  cosmetic findings are caveats in your reason, not blockers.
- **Restore every file you mutate** and verify SHA-256, restoring in a `finally`.
- **Clean up** every run, row, temp folder, process and advisory lock you create, and say what you
  removed.

## Recording your decision

Use **single quotes** around `--reason`, and keep apostrophes, backticks and `$` out of it.

```bash
# pass
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control approve . M30-approvals --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M30-approvals --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, the result for each of the nine attack items, and
your verdict.
