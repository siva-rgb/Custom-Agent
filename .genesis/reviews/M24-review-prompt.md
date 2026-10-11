You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**Use a fresh session: this round is for Opus 5, falling back to Fable 5.1** if Opus 5 fails to
start. M24 was implemented by Opus 5.5. Earlier reviews: Sonnet 5 approved M23, Fable 5.1 M22,
and Opus 5 M21a. Bring no memory of an earlier review into this one: if your session holds any,
say so and ask for a fresh one. Re-derive the requirements from SPEC.md ("#### M24": FR-95, FR-96;
NFR-24, NFR-25; AC-74; and the M24-dated sentences in FR-92 and FR-93) rather than inheriting the
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
- **Install:** `.venv\Scripts\python.exe -m pip install -r requirements.txt`. M24 adds no library
  (NFR-24, asserted by a test).
- **Host:** Windows Developer Mode must be on (D7).

**Only one test session at a time, and nothing else in parallel with one.** Every pytest session
compares the store's ids at its start and end (AC-44), and the development database is shared.
Check that no other python or node process is running first. Run your own probes with
`-p no:cacheprovider`: Genesis hashes .pytest_cache. The full suite takes about 6 minutes; keep
every command in the foreground and under 590 s.

**Gates:** `genesis gate` kills a gate after 120 s unless you pass `--timeout 590000`, and on this
host that kills only the venv launcher (KNOWLEDGE-64fa6e18). Run
`genesis gate . M24-claim-lock --timeout 590000`, then check for a stray python process.

**Mutations:** restore a mutated file byte for byte and verify its SHA-256 in a `finally`. Beware
`git stash`: on this host it rewrites line endings (KNOWLEDGE-ae83f412). Several files are CRLF.

**The FR-81 fixture is active in every test.** A store call made from a coroutine on the event
loop fails the test at teardown, so read the store from async probes with `asyncio.to_thread`.

**Concurrency probes:** at the default 5 ms thread switch interval a race can hide. Set
`sys.setswitchinterval(1e-6)` while a probe contends (KNOWLEDGE-93fa7f44). For cross-process
claims, use real OS processes on one database, as the gate's two-process test does.

## Task under review

**M24-claim-lock**, **round 1**, risk high. The last milestone of Phase 3 increment 1: the
claim-lock, and the exit signal (P3-D3).

### Decisions that shape it

- **P3-D3, P3-D10** (DECISION-39f28e48, DECISION-423f3e23).
- **DECISION-eecef939** (owner, M24 pre-flight). It repairs M23's review caveats 1 to 5
  (KNOWLEDGE-5d3124ff):
  1. Both ledger stores check that every cited version is in the claim's tenant and project, and
     raise the contribution's labels to cover what it cites. This makes migration 0011's comment
     true; the migration file itself is unedited.
  2. `supersede` takes its advisory locks in the order of their hash values, each once.
  3. Cite-by-URL ranks the run's own tool results above its briefed inputs.
  4. The key normalises NFC, then casefold, then NFC again.
  5. `record_claim` reads back only its own claim.

  It also sets example 21 to run offline (also a gate test on both stores) and live. The spec
  (FR-92, FR-93) was re-signed as `siva-rgb (owner)` with permission, after a `control requeue` of
  M24; the task list is unchanged.

## What changed

**Nothing since M23 is committed.** HEAD is `045696c` (M23). Review with:

```bash
git diff 045696c -- agentsdk tests scripts README.md SPEC.md
git status --short
```

- **`agentsdk/claimlock.py`** (new): `ClaimLock`, which works in layers.
  - Per key, an asyncio lock admits one task of a process at a time.
  - With a DSN, the admitted task then polls `pg_try_advisory_lock(hashtext(key), 4)`, taking a
    pooled connection only for each attempt, and keeps that connection as holder for its
    operation.
  - Waiters then run their own operation, which finds the holder's result.
  - A failed holder is retried by the next waiter with full jitter, up to 3 attempts; for its
    k-th attempt (k = 0, 1, 2) the delay is `random() * 0.2 * 2**k`. If every attempt fails, it
    and the waiters still queued raise `KeyFailed` naming the key.
  - `_completed` runs a thread's work to the end under cancellation, and undoes an acquired lock.
  - `ClaimLock.waits` counts the calls that waited.
- **`agentsdk/evidence.py`:** `ResourceCache.obtain` does serve-or-fetch-and-record under the lock.
  - The lock key is `fetch|tenant|project|ledger-if-SESSION|scope|canonical_uri|auth|variant`.
  - A NO_CACHE fetch takes no lock.
  - `SourceServed` and `SourceFetched` carry `wait: {seconds, took_result}` when the call waited.
- **`agentsdk/builtin_tools.py`:** `_fetch_recorded` delegates to `obtain`.
- **`agentsdk/ledger.py`:**
  - `RunLedger.record` runs under the lock key `claim|tenant|project|ledger|claim_key`, and
    `ClaimRecorded` carries `wait` when the call waited.
  - Caveats 1, 4 and 5: `_covered`, the evidence-backed `InMemoryLedgerStore(evidence)`, the
    double NFC, and the `claim()` store method.
- **`agentsdk/postgres.py`:** caveats 1, 2 and 5 (`_checked`, `_lock` by hash value,
  `claim()` / `_contributions()`).
- **`agentsdk/api.py`:**
  - one `ClaimLock` per Runner, given to each run's cache and ledger;
  - caveat 3 (`taken_in` order);
  - the in-memory ledger store is given the evidence store.
- **`scripts/21_contention.py`** (new). Its `offline(store, seed, tenants, project)` is also the
  gate test. **`README.md`:** a feature bullet, example 21's row, and a limit row replacing
  "concurrent misses each fetch".
- **`tests/test_claim_lock.py`** (new, the gate file, 27 tests). **Edits to approved tests:**
  `tests/test_distribution.py` lists `21_contention.py` (FR-56).

### Choices the spec left open, made by the author (judge them)

- **Waiters are serialised, not woken together.** Each runs its own operation after the holder,
  one at a time, and finds the holder's result: a cache hit, or a contribution to the existing
  claim. A holder's non-2xx response caches nothing, so the next waiter fetches for itself.
- **The retry state lives in one process.** Across processes, a waiter in another process simply
  takes its turn when the database lock frees. It is not told the holder failed, and retries on
  its own account. This is declared in the README.
- **Jitter exponent k counts from 0.** The bounds are 0.2, 0.4 and 0.8 s.
- **`supersede_claim` takes no claim-lock.** FR-95 names fetches and `record_claim`, and the
  store's per-key transaction lock already serialises supersessions.
- **The "took another's result" flag:** for a fetch, true when the call was served after it
  waited; for a claim, true when it waited and contributed to an existing claim.
- **Example 21's editor cites no source.** Live, the model made the fetch and the supersession in
  one response. A result of the same batch is not yet in the run's history (FR-93's declared
  rule), so citing the URL was refused. The script notes this.

## What the author ran

1. **The gate file.** Written alongside the implementation; 27 tests.
   - The Postgres exit-signal test first failed on its second seed: the database kept seed 0's
     fresh versions. Each repetition now uses its own project.
   - The records-at-once test makes the store write slow, so the records reliably overlap.
2. **Mutation run: 20 of 20 killed**, each file restored by SHA-256. The mutants covered:
   - no retry; no jitter; no growth in the delays; one attempt too many;
   - the key not marked failed; a holder's failure not noted;
   - `waited` never reported;
   - no cross-process lock; the database lock never released; a failed key kept forever;
   - fetch and `record_claim` taking no lock;
   - the fetch key ignoring the tenant;
   - the claim wait flag inverted;
   - each of the five caveats.
3. **Gates.** Both green on source hash `0c9970bd`, from one `genesis gate --timeout 590000` run.
   - `unit`: `tests/test_claim_lock.py`, exit 0, 27 passed in 28.36s.
   - `regression`: the full suite, exit 0, 1891 passed in 342.89s (06:14:57 to 06:20:41 UTC).
     The baseline before M24 was 1864.
4. **Example 21.**
   - Offline: 7 of 7, with about 31 claim-lock waits.
   - Live (gateway, Postgres, example.com and iana.org): 6 of 6 on the fourth attempt.
     - The first attempt failed: the planner's output limit cut off its eight-node plan, which is
       now given `max_output_tokens=8000`.
     - The second and third: the editor reworded its correction, then batched fetch and
       supersede together. The editor's instructions were tightened, and it now cites nothing.
     - The fourth: 8 readers, 8 lock waits, one claim per statement, and the chain readable.
   - Every attempt's rows were removed by run id, children included. The store is at baseline:
     example-tenant has 19 runs, there are 6 artifacts and 0 ledger rows or source versions, and
     no advisory lock is held.

## Attack these first

**Report a result for every item, `probed: <what you ran and what it showed>` or `not probed:
<why>`.** Save probes in a folder `m24-r1-probes` in your own session's scratchpad, with tenant ids
starting `SYN-m24r1`; delete only rows your probes wrote, by the run ids they recorded.

1. **Singleflight.** Many tasks and several OS processes on one key: exactly one fetch per page
   per reach? Any interleaving where two hold the same key, or a waiter fetches although the
   holder succeeded?
2. **Failure and retry.**
   - Exactly one retrier?
   - Delays within bounds?
   - At most 3 attempts?
   - Every queued waiter fails naming the key, and no other waiter retries?
   - A fresh call afterwards starts clean?
   - What happens when the holder is cancelled rather than raising, and when a waiter's own
     operation fails after a successful holder?
3. **Cancellation and timeouts.**
   - A cancelled waiter must not cancel the holder.
   - Cancellation at every await of `ClaimLock.run` and `_database`, including between
     `pg_try_advisory_lock` succeeding and its result arriving: is any advisory lock or pooled
     connection ever leaked? Check `pg_locks` and the pool's stats.
   - The tool timeout counting the wait.
4. **Cross-process.**
   - A holder's connection dropping (kill the process) releases the key.
   - A waiter never blocks a worker thread or holds a connection while it waits.
   - Do `hashtext` collisions between unrelated keys ever cost more than waiting?
5. **Tenancy (NFR-25).**
   - Can any wait, or result handed to a waiter, cross a tenant, a project, or (for SESSION) a
     ledger?
   - Read every lock key.
6. **The exit signal (FR-96, AC-74).**
   - Is example 21 a faithful exit signal?
   - Run it repeatedly, in memory and on Postgres, with your own seeds. Does it ever fail?
   - Are its checks strong enough, and does it contend in fact?
7. **The M23 caveats.**
   - Are 1 to 5 sound?
   - Can a direct store caller still record a cross-tenant citation or an under-tainted
     contribution?
   - Does the hash-ordered lock remove the crossing deadlock? Reproduce with colliding keys if you
     can.
8. **Governance and regressions.**
   - The re-signing (`git diff 045696c -- .genesis/project.json`).
   - The approved-test edit.
   - The full suite.

## Declared limitations: known, recorded, NOT findings

- **Retry state is per process** (README, Declared M24).
- **A hung holder keeps its key until its own tool timeout**; leases with expiry are Phase 6's
  (FR-95).
- **A holder's non-2xx response caches nothing, so its waiters fetch in turn.**
- **Results of other calls in the same parallel batch do not count** toward a claim's labels or
  citations (FR-93).
- Everything declared for M20 to M23 still stands (KNOWLEDGE-cc4d686c, KNOWLEDGE-afac039f,
  KNOWLEDGE-8e29cfe3, KNOWLEDGE-bbd190e1, KNOWLEDGE-2763f57b, KNOWLEDGE-5d3124ff), except the
  caveats this settles.

## Rules

- **Do not fix the code.** Report defects; the implementing session repairs them.
- **Gates are computed, never narrated.** Paste real command output for anything you assert.
- **Approve if it is sound.** A defect must be reachable and must matter. Latent, out-of-scope or
  cosmetic findings are caveats in your reason, not blockers.
- **Restore every file you mutate** and verify SHA-256, restoring in a `finally`.
- **Clean up** every run, row, temp folder, process and advisory lock you create, and say what you
  removed. The order is:
  1. ledger rows: citations, contributions, then claims (newest first);
  2. source versions and artifacts;
  3. plan states and plan versions;
  4. events, messages and manifests;
  5. runs.

## Recording your decision

Use **single quotes** around `--reason`, and keep apostrophes, backticks and `$` out of it.

```bash
# pass
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control approve . M24-claim-lock --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M24-claim-lock --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, the result for each of the eight attack items, and
your verdict.
