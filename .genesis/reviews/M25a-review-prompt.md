You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**Use a fresh session: this round is for Sonnet 5, falling back to Fable 5.1** if Sonnet 5 fails
to start. M25a was implemented by Opus 5.5. Earlier reviews: Opus 5 approved M25 and found the
defect this settles, Fable 5.1 approved M24a, and Opus 5 approved M24. Bring no memory of an
earlier review into this one: if your session holds any, say so and ask for a fresh one.
Re-derive the requirements from SPEC.md ("#### M25a": FR-109, and AC-82; and FR-99's W2 item,
which FR-109 corrects) rather than inheriting the framing below.

## Repository

`<repo>` is the folder that holds this file's repository; run everything from it.

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The PATH prefix is required: graphizer shells out to python3, and without the venv first on PATH
that hits the Microsoft Store alias and crashes Node.

- **Configuration:** both gates need DATABASE_URL from .env; the regression gate also needs
  BASE_URL and MODEL_API_KEY. **Never print credentials or a connection string.**
- **Host:** Windows Developer Mode must be on (D7). Python is 3.11, so `asyncio.wait_for` runs
  its own timer rather than `asyncio.timeout`.

**Running tests**
- **Only one test session at a time, and nothing else in parallel with one.** Every pytest session
  compares the store's ids at its start and end (AC-44), and the development database is shared.
- Check that no other python or node process is running first.
- Run your own probes with `-p no:cacheprovider`: Genesis hashes .pytest_cache.
- The full suite takes about 5 minutes; keep every command in the foreground and under 590 s.

**Gates:** `genesis gate` kills a gate after 120 s unless you pass `--timeout 590000`, and on this
host that kills only the venv launcher (KNOWLEDGE-64fa6e18). Run
`genesis gate . M25a-w2-window --timeout 590000`, then check for a stray python process.

**Mutations:** restore a mutated file byte for byte and verify its SHA-256 in a `finally`. Beware
`git stash`: on this host it rewrites line endings (KNOWLEDGE-ae83f412).

**The FR-81 fixture is active in every test.** A store call made from a coroutine on the event
loop fails the test at teardown.

## Task under review

**M25a-w2-window**, **round 1**, risk medium.

**Where it comes from.** M25 was approved at `d979634` with one finding (KNOWLEDGE-be223191), a
regression from M24a. The claim-lock's `_finished` read "the thread's future is done" as "this
exception is the future's own". So a cancellation delivered in the loop iteration where the
future had just completed was swallowed, and `_completed` returned the result. A timeout whose
deadline met the work's end then never fired, the executor's tool-timeout `wait_for` included.
The owner had it settled before M26 (DECISION-4cf55a17).

**Governance.**
- The spec and plan were re-signed as `siva-rgb (owner)` with permission.
- No state was edited by hand. M26 to M28 were paused through the CLI so that M25a is the active
  task; they are resumed and requeued after M25a.
- `plan reopen` was refused, because the paused M26 still counted as the active task. `spec
  approve` moved the workflow to planning anyway, so the task was added. Check that nothing else
  moved.
- After M25 was approved, the increment 2 plan's approval, which the `plan check` after
  DECISION-11af2ee2 had cleared, was re-signed (requeue M25, plan check, plan approve) before M25
  was completed.

### What changed

Only `agentsdk/claimlock.py`:
- `_finished` now asks `_own(future, exc)`: is `exc` the future's own outcome, meaning its
  cancellation or the very exception it failed with? Anything else was delivered. It is kept,
  with any non-cancellation exception outranking a cancellation, and raised after the undo.
- The non-cancellation branch's `if future.done(): continue`, the mutant M25's author dropped as
  equivalent, is gone with it.
- `SPEC.md`:
  - FR-109 and AC-82;
  - FR-99's W2 item now says its old sentence was false and points to FR-109;
  - the open-questions entry names FR-109.

Review with:

```bash
git diff d979634 -- agentsdk tests SPEC.md
git status --short
```

- `tests/test_w2_window.py` (new, the gate file, 9 tests). **Edits to approved tests:** none.

## What the author ran

1. **The new tests against M25's code:** 5 of the first 7 fail. Two more were added after the
   fix.
   - The two controls pass either way: a result with nothing delivered is returned, and the
     thread's own failure and cancellation are its own.
   - Each window is deterministic: the test hands `_completed` a future it completes itself, in
     the same iteration as the interruption.
   - The `wait_for` test freezes `loop.time` while both timers are scheduled. They then share a
     deadline, with the work's completion first in the heap.
2. **Mutation run: 4 of 5 killed.** Each file was restored by SHA-256, against the three
   claim-lock gate files of M24a, M25 and M25a.
   - The killed mutants include M25's bug itself and M25's dropped mutant.
   - The survivor is judged equivalent: it treats a cancelled future's own cancellation as
     delivered, and it still raises `CancelledError` with no undo either way. Judge whether that
     is right.
3. **Gates.** Both green on source hash `6ebfe367`, from one `genesis gate --timeout 590000` run.
   - `unit`: 9 passed in 0.52s.
   - `regression`: 1923 passed in 295.79s (14:20:19 to 14:25:16 UTC). The baseline before M25a
     was 1914.
4. **Store:** at baseline. 0 advisory locks.

## Attack these first

**Report a result for every item, `probed: <what you ran and what it showed>` or `not probed:
<why>`.** Save probes in a folder `m25a-r1-probes` in your own session's scratchpad, with tenant
ids starting `SYN-m25ar1`; delete only rows your probes wrote, by the run ids they recorded.

1. **The window, every way it opens.**
   - Cancellation, `asyncio.timeout`, `asyncio.wait_for` (on 3.11) and a non-cancellation
     exception, each delivered in the same iteration as the future completing with a result,
     failing, or being cancelled.
   - The acquire's `_completed` and the release's.
   - Is anything delivered ever returned past? Is the undo run exactly when it should be?
2. **`_own`.** Is "the future's own outcome" exactly identified?
   - Can a future's own `CancelledError` and a delivered one be told apart, and does it matter?
   - Can a delivered exception ever be the same object as the future's?
3. **C1 still holds.** Repeat M24a's repeated-cancellation probes against `pg_locks` and the pool,
   contended and not: no lock held, no connection out, and no return before the cleanup.
4. **The executor end to end.** A real fetch tool whose `timeout_seconds` expires while its
   claim-lock acquire's thread finishes: does the tool time out as reported?
5. **Regressions:** the gate files of M22 to M25 and the full suite.

## Declared limitations: known, recorded, NOT findings

- **W1, C6, B2, and a waited fetch that raises recording no wait** (FR-99).
- **`KeyFailed`'s forced `__suppress_context__` and its pickling,** carried from M25's review.
- **Everything declared for M20 to M25 still stands.**
- **M26 to M28 are not built.**

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
  control approve . M25a-w2-window --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M25a-w2-window --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, the result for each of the five attack items, and
your verdict.
