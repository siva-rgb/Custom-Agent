You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**Use a fresh session: this round is for Fable 5.1, falling back to Sonnet 5** if Fable 5.1 fails
to start. M24a was implemented by Opus 5.5. Earlier reviews: Opus 5 approved M24 and raised the
caveats this settles, Sonnet 5 approved M23, and Fable 5.1 approved M22. Bring no memory of an
earlier review into this one: if your session holds any, say so and ask for a fresh one.
Re-derive the requirements from SPEC.md ("#### M24a": FR-98, and AC-76) rather than inheriting
the framing below.

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
`genesis gate . M24a-claim-lock-caveats --timeout 590000`, then check for a stray python process.

**Mutations:** restore a mutated file byte for byte and verify its SHA-256 in a `finally`. Beware
`git stash`: on this host it rewrites line endings (KNOWLEDGE-ae83f412).

**The FR-81 fixture is active in every test.** A store call made from a coroutine on the event
loop fails the test at teardown.

## Task under review

**M24a-claim-lock-caveats**, **round 1**, risk medium. M24 was approved with caveats
(KNOWLEDGE-4301be95). The owner had C1 to C4 settled before Phase 3 increment 2 is specified
(DECISION-a13245a0), as FR-98 and AC-76. The plan was reopened through the CLI with no task
active, and the spec and plan were signed as `siva-rgb (owner)` with permission. No state was
edited by hand.

### What it settles

- **C1: cleanup under repeated cancellation.**
  - Before: `_completed` suppressed a re-delivered `CancelledError`. A second cancellation while
    acquiring, holding or releasing the database lock left the advisory lock unlocked by no code
    and the pooled connection never returned.
  - Now: `_finished` waits for a thread's future to finish however many cancellations arrive.
    `_completed` then applies the undo, itself waited for the same way, before re-raising
    `CancelledError`. The normal release in `_database`'s `finally` goes through it too.
- **C3 and C4: the attempt budget.**
  - Before: a cancelled attempt, or a waiter's own failure after a successful holder, reopened
    the three-attempt budget.
  - Now: `_Key.budget` is opened at the first failure and lasts while any call is on the key.
    Every retry attempt draws on it, a cancelled one included; a waiter's own failure draws on it
    too. When it is spent, the key fails for every call still on it.
  - It is fresh only once no call is left on the key.
- **C2: key encoding.** Lock keys are `lock_key(*parts)`, a JSON array, so parts joined by a
  delimiter can no longer collide.
  - Used for the fetch key in `evidence.py` and the claim key in `ledger.py`.
  - The M23 transaction-lock names in `PostgresLedgerStore._lock` still join with `|`. A collision
    there only serialises two writers (hashtext collisions already do the same), and changing it
    would edit M24's approved caveat-2 test. Judge whether that is acceptable.

## What changed

**Nothing since M24 is committed.** HEAD is `64a262d` (M24). Review with:

```bash
git diff 64a262d -- agentsdk tests SPEC.md
git status --short
```

- `agentsdk/claimlock.py`: `lock_key`, `_Key.budget` and `failed_once`, the `_retry` budget,
  `_finished`, and `_completed`.
- `agentsdk/evidence.py` and `agentsdk/ledger.py`: keys built with `lock_key`.
- `SPEC.md`: an M24a section with FR-98, and AC-76.
- `tests/test_claim_lock_followup.py` (new, the gate file, 9 tests). **Edits to approved tests:**
  none.

## What the author ran

1. **The new tests against M24's code:** 7 of 9 fail.
   - That run swapped in HEAD's three source files, aliased `lock_key` to a pipe join, then
     restored everything by SHA-256.
   - The fresh-budget test is a control and passes either way.
   - Two test flaws were found and fixed on the way. The acquiring window first slept *before*
     taking the lock, so it measured before the leak happened. The pool baseline was taken while
     the pool was still opening a connection.
2. **Mutation run: 7 of 7 killed**, each file restored by SHA-256.
   - "Undo itself cut off by cancellation" survived twice. The fix was a test for the instant a
     cancelled call returns, made with a canceller that stops once the task is done. A release
     thread cannot be stopped, so the observable defect is the call returning before its lock is
     released.
   - "A cancelled attempt costs nothing" was killed by a hang (the retrier loops forever), not an
     assertion.
3. **Gates.** Both green on source hash `880b57dd`, from one `genesis gate --timeout 590000` run.
   - `unit`: 9 passed in 4.59s.
   - `regression`: 1900 passed in 358.15s (10:41:23 to 10:47:22 UTC). The baseline before M24a
     was 1891.
4. **Store:** at baseline. No advisory lock is held.

## Attack these first

**Report a result for every item, `probed: <what you ran and what it showed>` or `not probed:
<why>`.** Save probes in a folder `m24a-r1-probes` in your own session's scratchpad, with tenant
ids starting `SYN-m24ar1`; delete only rows your probes wrote, by the run ids they recorded.

1. **C1.** Cancel at every await of `ClaimLock.run`, `_database` and `_completed`, once, twice and
   many times, contended and not.
   - Is any advisory lock (check `pg_locks`) or pooled connection (check the pool stats) ever left
     behind?
   - Does a cancelled call ever return before its cleanup finishes?
   - Can `_finished` loop forever? Consider a future that never completes, or a thread that
     hangs, and say whether that matters.
   - Is the `CancelledError` re-raised in every case, and does that interact correctly with
     `asyncio.timeout` and the executor's tool timeout, which count cancellations?
2. **C3 and C4.**
   - Is "at most three attempts after a failure" now absolute? Combine cancellation, a waiter's own
     failure, and successes in between.
   - Does a key ever fail when it should not, or retry when it should not?
   - What happens to `budget` after a success that follows a failure?
3. **C2.** Can any two different reaches still share a claim-lock key? Is the M23 transaction-lock
   naming an acceptable leftover?
4. **Regressions:** M24's gate file (including the exit signal on both stores), M22 and M23's gate
   files, and the full suite.

## Declared limitations: known, recorded, NOT findings

- **C5 to C7, and example 21's vacuous taint check on the uncited correction,** are carried to
  increment 2 (KNOWLEDGE-4301be95, DECISION-a13245a0).
- **Retry state is per process; a hung holder keeps its key until its tool timeout.** Both are
  declared in M24.
- Everything declared for M20 to M24 still stands, except the caveats this settles.

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
  control approve . M24a-claim-lock-caveats --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M24a-claim-lock-caveats --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, the result for each of the four attack items, and
your verdict.
