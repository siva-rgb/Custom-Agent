You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**Use a fresh session: this round is for Fable 5.1, falling back to Opus 5** if Fable 5.1 fails
to start. M28 was implemented by Opus 5.5. Earlier reviews: Sonnet 5 approved M27, Opus 5
approved M26a, Fable 5.1 approved M26, and Sonnet 5 approved M25a. Bring no memory of an earlier
review into this one: if your session holds any, say so and ask for a fresh one. Re-derive the
requirements from SPEC.md rather than inheriting the framing below:
- "#### M28": FR-107, FR-108, AC-80 and AC-81;
- NFR-25 to NFR-28, which the task lists;
- FR-104, FR-105 and AC-79 as amended at this task's pre-flight.

Read the increment 2 decision table, DECISION-2cec3b61, and this task's two owner decisions:
DECISION-df1790f1 (M27's caveats, and an answer-only correction is valid) and
DECISION-4f1acdb2 (what min_outcomes counts, unvalidated claims, example 23 live).

## Repository

`<repo>` is the folder that holds this file's repository; run everything from it.

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
  A live example writing to the database during a session fails it too.
- Check that no other python or node process is running first.
- Run your own probes with `-p no:cacheprovider`: Genesis hashes .pytest_cache.
- **The full suite takes about 7 minutes.**

**Gates (DECISION-066e317a).** Run `genesis gate . M28-calibration-and-exit --timeout 1500000` in
the background, or detached, since it outlasts a 10-minute foreground call. Then check for a
stray python process. If a gate reads `stale` straight after, the first run churned
.pytest_cache: run the gate file once with the cache on, then gate again.

**Mutations:** restore a mutated file byte for byte and verify its SHA-256 in a `finally`. Beware
`git stash`: on this host it rewrites line endings (KNOWLEDGE-ae83f412). Do not mutate an applied
migration file, because its checksum is recorded in the database.

**The FR-81 fixture is active in every test.** A store call made from a coroutine on the event
loop fails the test at teardown.

## Task under review

**M28-calibration-and-exit**, **round 1**, risk **medium**. HEAD is `1964f7c` (M27). This task
closes Phase 3's second increment. Its pre-flight settled M27's review caveats inside this task,
as the owner chose, rather than in an M27a (KNOWLEDGE-98fb16e9).

### Pre-flight: M27's caveats (`agentsdk/orchestrator.py`, `tests/test_critic.py`)

1. **The critic's label clauses of AC-79, now demonstrated.** A new test taints the critic's
   result alone, with a flag neither the page nor any answer carries. It then checks that each of
   these carries it, for approved, corrected, escalate and a failed critic, on both stores:
   - the corrected answer, and its stored output;
   - the superseding claim;
   - every `critic` verification;
   - the orchestrator's reports.

   M27's reviewer found four NFR-28 mutants surviving every test, among them dropping `taking_in`
   at the corrected return. Every taint check compared against the page, whose taint the critic
   shares.
2. **Text the ledger cannot record is refused at the verdict schema.** The reason and every
   statement carry a `pattern`, `_RECORDABLE`: some non-whitespace, and no NUL or lone
   surrogate. Before, a whitespace-only or NUL reason passed `minLength`, failed `NewContribution`
   outside the `try`, and left a critic run with no critic row and no `CriticVerdict`.
3. **Paraphrases name only the node's claims:** none at all for a node with fewer than two.
4. **An answer-only correction is valid** (owner decision). `corrected` with `corrections: []`
   leaves the node `done` with the corrected answer, tainted as both, and supersedes nothing.
   This was already the behaviour; it is now stated in FR-105 and tested.
5. **A store failure after the critic, on PostgreSQL too.** The patch is on the store class,
   since PostgreSQL hands out a fresh store object per call. The execution fails `node_error`
   with no verdict recorded, and the rerun has its own one critic run.

### M28 itself

- **`agentsdk/calibration.py` (new):**
  - `THRESHOLDS`;
  - `calibrate(rows, ...)`, pure;
  - `CalibrationReport`, frozen, with `to_json()`.

  The unit is a (gate row, judged claim) pair, against the claim's latest external outcome. Two
  outcomes at one instant read as `incorrect`, because the stores order a tie differently.
- **`Runner.calibration_report`** (`agentsdk/api.py`) checks its arguments, reads
  `verifications_since` on a worker thread, and computes. It writes nothing.
- **`scripts/23_gate_contention.py`** (FR-108) is example 21's eight readers, every node gated.
  - **What the corpus fixes:** every reader states each fact at the same confidence. Readers of
    the second page state it at 0.4. Readers 0 and 4 state a wrong claim at 0.3. So the first
    executions that fire are known before the run: {reader-0, reader-1, reader-4, reader-5}.
  - **The forced race:** the scripted critic of each node shown the wrong claim waits until
    both are briefed, then corrects it. One correction wins. The other is refused "already
    superseded", fails its node, and the planner replans that node without the wrong claim.
  - **Afterwards** it records outcomes for every claim, prints the report, and checks it changed
    nothing.
- **`tests/test_calibration.py`** (the unit gate) has 16 tests: AC-80's arithmetic over a
  hand-computed fixture, the Runner method on both stores, AC-81 ten times on both stores,
  example 23 offline, and NFR-27.

### Edits to approved tests (each to judge)

- **`tests/test_critic.py`:** five tests added, and none of its existing 21 changed.
- **`tests/test_distribution.py`:** `EXPECTED_EXAMPLES` gains `23_gate_contention.py`.

## What changed

**Nothing since M27 is committed.** Review with:

```bash
git diff 1964f7c -- agentsdk tests scripts README.md SPEC.md
git status --short
```

## What the author ran

1. **Tests first.**
   - The pre-flight tests ran against M27's code first:
     - the schema and paraphrase tests failed, as expected;
     - the label, answer-only and store-failure tests passed, since the behaviour was already
       right and only undemonstrated.
   - `tests/test_calibration.py` was written before `agentsdk/calibration.py` existed.
   - Test bugs found on the way:
     - a node carried into version 2 is reported twice;
     - a `Runner` needs a model client;
     - the race check counted a refused correction twice, since it has both a verdict and a
       `critic_failed` reason.
   - **An implementation bug found on the way:** the first `_RECORDABLE` used `\S` for its
     required character. `\S` itself matches a NUL or a lone surrogate. The pattern was then
     checked against the ledger's own `_text` over every code point, alone, after a letter and
     before a space: 0 mismatches.
2. **Mutation runs:**
   - **Pre-flight: 13 of 13 killed,** among them the reviewer's `taking_in` survivor and the
     other label mutants. One mutant, "paraphrases may name any string", first survived. The
     test's node had one claim, so `maxItems: 0` refused the group before the enum was checked.
     The node now has two claims.
   - **Calibration and exit: 17 of 17 killed.** One, "critic verdicts counted as units", first
     survived only because it was written at confidence 0.99, which no threshold flags; at no
     confidence it was killed. Two candidate mutants are equivalent and were not run:
     - counting a node's outcome into `latest`, since no claim matches it;
     - dropping the `kinds` filter, since every kind is read anyway.

   Every file was restored by SHA-256.
3. **Example 23 live**, three runs on the gateway and PostgreSQL. The rows of all three were
   removed by run id.
   - **Run 1:** the gate fired for exactly the four nodes the corpus fixes. Two FR-96 checks
     failed: a real critic superseded two shared claims, restating them as its own, and the
     checks read only active claims. They now read the claims as the readers recorded them.
   - **Run 2:** two real corrections lost their race to another node's critic. Both nodes
     replanned, and each rerun was judged once. The page-fact check failed again: the v2 reruns
     re-recorded three statements that critics had superseded, which correctly made new claims
     (FR-92).
     - **Live** now checks that at most one claim of a statement is active and every reader is
       among its claims' contributors.
     - **Offline** keeps the exact one-claim check, because there no critic touches a fact.
   - **Run 3:** all 9 live checks passed.
4. **Gates**, both green on source hash `46d03d73`, run in the background with `--timeout
   1500000`. The first run went stale between unit and regression from cache churn, so it was
   rerun.
   - `unit`: 16 passed in 37.54s.
   - `regression`: 2034 passed in 448.04s (16:52:07 to 16:59:36 UTC). The baseline before M28
     was 2010.
5. **Genesis:** the spec and plan were re-signed as owner (DECISION-df1790f1). M28 was requeued
   and reactivated by the plan approval, because the spec approval had cleared the active-task
   pointer.

## Attack these first

**Report a result for every item, `probed: <what you ran and what it showed>` or `not probed:
<why>`.** Save probes in a folder `m28-r1-probes` in your own session's scratchpad, with tenant ids
starting `SYN-m28r1`; delete only rows your probes wrote, by the run ids they recorded.

1. **The report's arithmetic (AC-80).**
   - Recompute a report by hand over your own rows on both stores: each signal, each threshold,
     the suggestion, `claims_with_outcome`.
   - Is the unit what FR-107 states, a claim counted once per gate row?
   - Are claims outside a plan, node outcomes and unjudged claims left out where they should be
     and named where they should be?
   - Is the tie rule sound, and does any ordering of rows change a result?
2. **Changes nothing, reads one tenant and project (P3-D19, NFR-25).** Can a report write a row,
   change a `GatePolicy`, or read another tenant's or project's verification?
   - Consider `since`.
   - Consider a project sharing a tenant.
   - Consider rows whose `detail` an application wrote through `record_validation`.
3. **The exit signal (AC-81).**
   - Is the expected set of critic runs truly fixed by the corpus and not read back from the run?
   - Can the race go unexercised while the checks still pass?
   - Do the taint checks actually compare against what each item rests on, or only against the
     page again (the recurring gap KNOWLEDGE-98fb16e9 names)?
   - Shuffle starts and delays beyond the test's ten seeds.
4. **The pre-flight fixes.**
   - Does `_RECORDABLE` refuse exactly what the ledger refuses? Over-refusal is a defect too
     (KNOWLEDGE-79add1a1).
   - Can any valid verdict still fail to be recorded?
   - Does the new label test fail for each label the critic must taint?
5. **Records and events under the race.** For the losing node, compare its `critic`
   verifications, its `CriticVerdict`, the ledger, and the node's status. Is `applied`/`refused`
   honest? Check both stores.
6. **The approved-test edits listed above.**
7. **NFR-27 and NFR-29.** No runtime dependency added. An ungated plan is unchanged.
8. **Example 23 live,** if you can afford the gateway calls. Clean up by run id.
9. **Regressions:** the full suite, and the gate files of M22 to M27.

## Declared limitations: known, recorded, NOT findings

- **Everything declared for M20 to M27.** The M27 review caveats are settled here, not declared.
- **Live verdicts and confidences are the models' own.** Example 23 live checks only what must
  hold whatever they decide (DECISION-4f1acdb2); the expected set is checked offline.
- **The suggestion is advisory by design.** Nothing applies it (P3-D19, I-23).

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
  control approve . M28-calibration-and-exit --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M28-calibration-and-exit --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, the result for each of the nine attack items, and
your verdict.
