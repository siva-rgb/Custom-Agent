You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**Use a fresh session: this round is for Opus 5, falling back to Sonnet 5** if Opus 5 fails to
start. M25 was implemented by Opus 5.5. Earlier reviews: Fable 5.1 approved M24a, Opus 5 approved
M24, and Sonnet 5 approved M23. Bring no memory of an earlier review into this one: if your
session holds any, say so and ask for a fresh one. Re-derive the requirements from SPEC.md
("#### M25": FR-99, and AC-77; and NFR-25, NFR-26 and NFR-29, which the task also lists) rather
than inheriting the framing below.

## Repository

`<repo>` is the folder that holds this file's repository; run everything from it.

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The PATH prefix is required: graphizer shells out to python3, and without the venv first on PATH
that hits the Microsoft Store alias and crashes Node.

- **Configuration:** both gates need DATABASE_URL from .env; the regression gate also needs
  BASE_URL and MODEL_API_KEY. **Never print credentials or a connection string.**
- **Host:** Windows Developer Mode must be on (D7).

**Running tests**
- **Only one test session at a time, and nothing else in parallel with one.** Every pytest session
  compares the store's ids at its start and end (AC-44), and the development database is shared.
- Check that no other python or node process is running first.
- Run your own probes with `-p no:cacheprovider`: Genesis hashes .pytest_cache.
- The full suite takes about 6 minutes; keep every command in the foreground and under 590 s.

**Gates:** `genesis gate` kills a gate after 120 s unless you pass `--timeout 590000`, and on this
host that kills only the venv launcher (KNOWLEDGE-64fa6e18). Run
`genesis gate . M25-gate-carried-items --timeout 590000`, then check for a stray python process.

**Mutations:** restore a mutated file byte for byte and verify its SHA-256 in a `finally`. Beware
`git stash`: on this host it rewrites line endings (KNOWLEDGE-ae83f412).

**The FR-81 fixture is active in every test.** A store call made from a coroutine on the event
loop fails the test at teardown.

## Task under review

**M25-gate-carried-items**, **round 1**, risk medium. It is the first milestone of Phase 3's
second increment, specified as FR-99 to FR-108 from owner decisions P3-D13 to P3-D22
(DECISION-f9888168, DECISION-581a647b), after a read-only spec review (KNOWLEDGE-8710b0e2) whose
further choices the owner accepted (DECISION-2cec3b61). M25 settles the caveats carried from M24
and M24a (KNOWLEDGE-4301be95, KNOWLEDGE-06a8cf2f). The four increment 2 tasks were added with
their regression gate dropped by a shell-quoting slip; the owner chose one manual edit to
`.genesis/project.json` to add it, recorded as DECISION-11af2ee2. Check that edit changed nothing
else.

### What it settles

All in `agentsdk/claimlock.py` unless named.

- **B1, an attempt counts once its operation starts.** `_retry` draws on the key's budget after
  the backoff, so a waiter cancelled in its backoff spends nothing and the next waiter retries. A
  backoff that raises still spends its attempt, or the loop would never end. This amends FR-98's
  "an attempt cut off by cancellation counts as one".
- **W2, nothing delivered to a waiting task is swallowed.** `_finished` returns what interrupted
  the caller instead of a flag. An exception raised while the thread's future is still running
  was delivered to the caller: it is kept, and `_completed` raises it once the work is finished
  and undone. One raised once the future is done is the future's own outcome.
- **K1, every `KeyFailed` carries its cause.** The cause is the failure that spent the last
  attempt, set as `__cause__` in the constructor and raised `from` it, for the retrier, for a key
  failed by a waiter's own failure, and for a cancelled last attempt.
- **P1** (`agentsdk/postgres.py`, `PostgresLedgerStore._lock`): lock names are built with
  `lock_key`.
- **P2** (`agentsdk/manifest.py`): `agent_spec_hash` is `v2:` plus the SHA-256 of a JSON array of
  the id, the instructions and the sorted profile. Every spec's hash changes once.
- **C5** (`agentsdk/evidence.py`, `agentsdk/events.py`): a fetch that waited and got a non-2xx
  response emits `SourceUncached` with `canonical_uri`, `status` and the wait (`took_result`
  false).
- **Example 21** (`scripts/21_contention.py`):
  - the editor's correction cites the page version the wrong claim rests on, by its uri, in one
    call;
  - the taint check is a function, `taint_holds`, that is False for no claims;
  - the check is folded into one named check that also requires the correction to cite a page,
    so the example still prints seven checks.
- **Declared:** W1, C6 and B2. The README's limits list them, and the P1/P2 versioning note.
  W1 and C6 are asserted in the gate file; B2 by M24a's `test_c4`.
- **C7**, bookkeeping: the task lists FR-99, AC-77, NFR-25, NFR-26 and NFR-29.

## What changed

**Nothing since M24a is committed.** HEAD is `d339d28`. Review with:

```bash
git diff d339d28 -- agentsdk tests scripts README.md SPEC.md
git status --short
```

- `tests/test_gate_carried.py` (new, the gate file, 14 tests).
- **Edits to approved tests:** one. `tests/test_claim_lock.py`'s
  `test_caveat2_keys_are_locked_in_the_order_of_their_lock_values_each_once` now computes the
  expected names with `lock_key` (P1, listed by FR-99), and imports it. No other approved test is
  edited.
- `SPEC.md`: the whole increment 2 section, written and approved in this cycle.

## What the author ran

1. **The new tests against the unrepaired code:** 11 of 14 fail. The others:
   - W1 and C6 are declared behaviour, so they pass either way;
   - the thread's own failure is a control;
   - the failed-backoff test was added later.

   Two test flaws were found and fixed. A pool stats probe called `pool.wait()` while the
   holder had the pool's only connection, and timed out. The W2 test would have left a lock held
   when the unrepaired code swallowed the interrupt, so it now releases it.
2. **Mutation run: 16 of 16 killed**, each file restored by SHA-256.
   - "A failed backoff spends nothing" was killed by a hang, not an assertion.
   - "The taint check ignores taint flags" first survived: the clean case was also trusted. It
     was killed by an untrusted, unflagged claim.
   - One mutant was judged equivalent and dropped: removing `_finished`'s `if future.done():
     continue` for a non-cancellation exception. When the work itself raised, both paths raise
     that same exception. Judge whether that is right.
3. **Example 21 live, twice, against the gateway on PostgreSQL.**
   - First, with the editor told to fetch the page and then cite it by URL in two turns, the
     model batched both. The citation was refused (a result of the same batch is not yet in
     history, FR-93), and it recorded the right statement as a new claim: the history check
     failed.
   - With a single call citing the version uri, every check passed.
   - Both runs' rows were removed by their run ids.
4. **Gates.** Both green on source hash `10b16da7`, from one `genesis gate --timeout 590000` run.
   - `unit`: 14 passed in 2.57s.
   - `regression`: 1914 passed in 327.80s (13:06:43 to 13:12:12 UTC). The baseline before M25 was
     1900.
5. **Store:** at baseline. No advisory lock is held.

## Attack these first

**Report a result for every item, `probed: <what you ran and what it showed>` or `not probed:
<why>`.** Save probes in a folder `m25-r1-probes` in your own session's scratchpad, with tenant ids
starting `SYN-m25r1`; delete only rows your probes wrote, by the run ids they recorded.

1. **B1 and FR-98 together.** Is "at most three attempts after a failure" still absolute?
   - Combine cancellation in a backoff, cancellation during an operation, a waiter's own failure,
     and a backoff that raises.
   - Can a key now retry forever, for example when every waiter is cancelled in its backoff?
2. **W2.** Does `_finished`/`_completed` still meet FR-98's C1 under repeated cancellation?
   - Cancel at every await, once, twice and many times, with `pg_locks` and pool stats checked.
   - What happens when a cancellation and another exception both arrive, in either order?
   - Is the `CancelledError` re-raised the one that was delivered, and does `asyncio.timeout`
     still convert it?
   - Is the dropped mutant truly equivalent?
3. **K1.** Every path that raises `KeyFailed`: is `__cause__` the failure that spent the last
   attempt, never an earlier `KeyFailed`? Does setting `__cause__` in `__init__` misbehave
   anywhere else, for example pickling or the message?
4. **P1, P2 and NFR-29.**
   - Any remaining delimiter join feeding a hash or a lock name?
   - Does any reader of `agent_spec_hash` assume 64 hex characters?
   - Is everything else about an ungated run unchanged?
5. **C5.** Is `SourceUncached` emitted exactly for a waited fetch answered non-2xx, on both
   stores, and never for an unwaited one or a 2xx? Is its payload storable and tenant-safe
   (NFR-25)?
6. **Example 21.** Is the taint check genuinely not vacuous for the correction? Does the
   one-call citation of a version uri hold the claim to its tenant and project?
7. **Regressions:** the gate files of M22, M23, M24 and M24a, and the full suite.

## Declared limitations: known, recorded, NOT findings

- **W1, C6 and B2,** declared in FR-99 and the README, and **a waited fetch that raises recording
  no wait.**
- **Everything declared for M20 to M24a still stands,** except the caveats this settles.
- **Increment 2's later milestones (M26 to M28) are not built,** so their requirements are not
  findings here.

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
  control approve . M25-gate-carried-items --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M25-gate-carried-items --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, the result for each of the seven attack items, and
your verdict.
