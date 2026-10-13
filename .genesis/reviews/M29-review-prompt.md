You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**Use a fresh session: this round is for Opus 5, falling back to Sonnet 5** if Opus 5 fails to
start. M29 was implemented by Opus 5.5. Earlier reviews: Fable 5.1 approved M28, Sonnet 5
approved M27, Opus 5 approved M26a, and Sonnet 5 reviewed this increment's spec draft. Bring no
memory of an earlier review into this one: if your session holds any, say so and ask for a fresh
one.

Re-derive the requirements from SPEC.md rather than inheriting the framing below:
- "#### M29": FR-111 and FR-112;
- AC-84 and AC-85;
- NFR-29, NFR-31 and NFR-32, which the task lists.

Read the increment's decision table (P4-D1 to P4-D15), and the owner decisions DECISION-005d0bfd,
DECISION-af27465a and DECISION-d9ddaf0f (the spec-level choices accepted). Read KNOWLEDGE-3be8acc4,
M28's review, whose four carried items FR-111 settles.

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
- **The full suite takes about 7 minutes.**

**Gates (DECISION-066e317a).** Run `genesis gate . M29-carried-and-records --timeout 1500000` in
the background, or detached, since it outlasts a 10-minute foreground call. Then check for a stray
python process. If a gate reads `stale` straight after, the first run churned .pytest_cache: run
the gate file once with the cache on, then gate again.

**Mutations:** restore a mutated file byte for byte and verify its SHA-256 in a `finally`. Beware
`git stash`: on this host it rewrites line endings (KNOWLEDGE-ae83f412). **Do not mutate an
applied migration file, `0014` included:** its checksum is recorded in the database.

**The FR-81 fixture is active in every test.** A store call made from a coroutine on the event
loop fails the test at teardown.

## Task under review

**M29-carried-and-records**, **round 1**, risk **medium**. HEAD is `8c7345f` (M28); the Phase 4
spec, its decisions and this task are uncommitted on `phase-4`. **M29 must change no behaviour:**
nothing writes the new tables until M30.

### FR-111: M28's carried items

1. **`_verdict_schema`** (`agentsdk/orchestrator.py`):
   - `corrected_answer` carries `_STORABLE` unconditionally, so the empty string passes but no NUL
     or lone surrogate does;
   - under the `corrected` branch it carries `_RECORDABLE` as well;
   - the item schemas of empty claim and source lists, never reachable because `maxItems` is 0,
     now carry a pattern too.

   A test walks the schema for a string node with no enum or pattern.
2. **`calibrate`** (`agentsdk/calibration.py`) leaves `no_claims` out of `signals` and reports
   `no_claims_fired`. Its critic table counts a claim's critic row only when the gate row of the
   same node execution was read. That execution is found through the critic's node row with the
   same `critic_run`.
3. **NFR-29 pinned:** `tests/fixtures/nfr29_ungated_plan.json` was generated from the unchanged
   code before any M29 edit, and was identical across two runs. A test compares two runs of a
   fixed two-node sequential plan against it, after normalisation: an ungated run, and a run under
   a `GatePolicy()` that gates nothing.
4. **Declared** in the README: example 23's taint checks cannot tell a missing critic label.

### FR-112: the record

- **Migration `agentsdk/migrations/0014_interruptions.sql`:**
  - `runs` gains `UNIQUE (run_id, tenant_id, project_id)` (`runs_tenancy_identity`);
  - three new tables: `interruptions`, `interruption_decisions` (a unique interruption id) and
    `standing_decisions` (a `seq`);
  - `execution_manifests.approval_timeout`, NULL until M30.
- **`agentsdk/outcomes.py`:** `RunInterruption` gains defaulted fields.
- **`agentsdk/interruptions.py` (new):**
  - the types `InterruptionDecision`, `StandingDecision` and `InterruptionView`;
  - the four exceptions;
  - `checked_interruption` and `state_of`;
  - `InMemoryInterruptionStore`.
- **`agentsdk/postgres.py`:** `PostgresInterruptionStore`.
- **Wiring:** `Persistence.interruption_store()` and `Runner._interruption_store()`.

### Edits to approved tests (each to judge)

- **`tests/test_calibration.py`:**
  - the `signals` assertion: `no_claims` is dropped, and `no_claims_fired` is asserted;
  - the `critic()` fixture now writes a node row and a claim row sharing a `critic_run`, as the
    orchestrator writes them, because the join needs the node row;
  - `CRITICS` and `flood` follow.
- **`tests/conftest.py`:** `_TRACKED` gains the three tables.
- **`tests/test_critic.py`:** one test added (an unstorable corrected answer is re-asked and
  records no verdict); no existing test changed.

## What changed

```bash
git diff 8c7345f -- agentsdk tests README.md SPEC.md
git status --short
```

## What the author ran

1. **Baseline:** 2034 passed in 409.62s before any change, the same as M28.
2. **Tests first.**
   - `tests/test_interruption_record.py` was written before `interruptions.py` and `0014` existed.
     It now has 24 tests, both stores where a store is involved.
   - The NFR-29 fixture was generated before any code was edited, by a script that execs the test
     module's own recorder in memory.
   - **A test bug found on the way:** the database's foreign-key check was first tried on an
     interruption already decided, so the unique key fired first; the check now uses an undecided
     one.
3. **Mutation run: 20 of 20 killed,** each file restored by SHA-256. One first survived, "an
   interruption of an unknown run accepted": every wrong-tenant case also had a wrong top-level
   run, so the top-level run check alone refused them. A case with a valid top-level run and a run
   of another tenant now kills it. The mutants cover:
   - the report: `no_claims` and the critic join;
   - the verdict schema's three patterns;
   - the in-memory store's refusals, tenancy checks, expiry state and listing beneath a run;
   - the principal rules;
   - the PostgreSQL store's duplicate-decision path, its run check and its recursive listing.
4. **Gates**, both green on source hash `9b084719`, run in the background with `--timeout
   1500000`. The first run went stale between unit and regression, because the author wrote this
   prompt while it ran; it was rerun with nothing changing.
   - `unit`: 24 passed in 2.31s.
   - `regression`: 2060 passed in 406.61s (11:17:43 to 11:24:31 UTC). The baseline was 2034: 24
     tests from the gate file, and two from `tests/test_critic.py`'s new parametrised test.

## Attack these first

**Report a result for every item, `probed: <what you ran and what it showed>` or `not probed:
<why>`.** Save probes in a folder `m29-r1-probes` in your own session's scratchpad, with tenant ids
starting `SYN-m29r1`. Delete only rows your probes wrote, by the run ids they recorded:
interruptions and decisions before their runs.

1. **No behaviour change (NFR-32).**
   - Does anything write the new tables, or change a request or event, before M30?
   - Is the NFR-29 fixture meaningful: would it catch a changed request or event? Mutate something
     an ungated plan sends and see.
   - Is the normalisation too broad? It excludes fields by name and substitutes every UUID.
2. **Tenancy (NFR-31).** Try to record, read or decide across tenant or project on both stores,
   through the store API and through raw SQL. Consider the top-level run, the standing decision a
   decision names, and listing "beneath" a run.
3. **One decision per interruption.** Race two `append_decision` calls on PostgreSQL and in
   memory. Is exactly one recorded, and does the loser raise `InterruptionAlreadyDecided`, not a
   driver error?
4. **The schema.**
   - Do `0014`'s keys and checks enforce what FR-112 states: the composite keys, the
     source/principal/standing checks, and expiry or cancel being a reject?
   - Is `0014` idempotent?
   - Does it apply on a database holding M28's rows?
5. **Types and states.**
   - Can a decision, standing decision or interruption be built that FR-112 forbids?
   - Is `state_of` right at the expiry boundary?
   - Do `InterruptionView`s read back equal on both stores?
6. **FR-111.**
   - Does the verdict schema still accept every valid verdict? Over-refusal is a defect too
     (KNOWLEDGE-79add1a1).
   - Is the critic join right when a node is rerun: a new version, a new gate row?
   - Is `no_claims_fired` what FR-111 says?
7. **The approved-test edits listed above.**
8. **Regressions:** the full suite, and the gate files of M26 to M28.

## Declared limitations: known, recorded, NOT findings

- **Everything declared for M20 to M28.**
- **What M29 leaves to M30:** the approval behaviour itself, `Runner.decide` and the run-side
  `waiting` flag. `InterruptionView.waiting` is always False from a store.

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
  control approve . M29-carried-and-records --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M29-carried-and-records --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, the result for each of the eight attack items, and
your verdict.
